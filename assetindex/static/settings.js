'use strict';

// "设置" dialog: the vision model that "看图描述" and "素材整理" both use, and the
// embeddings API behind meaning search. Loaded after app.js.

const PAGE_HEADERS = { 'X-Requested-With': 'fetch', 'Content-Type': 'application/json' };
const JOB_LIMITS = ['workers', 'retries', 'retry_wait', 'max_failures'];

async function errorText(resp) {
  try {
    const body = await resp.json();
    if (typeof body.detail === 'string') return body.detail;
  } catch { /* not JSON */ }
  return `HTTP ${resp.status}`;
}

async function modelFetch(url, options = {}) {
  const resp = await fetch(url, options);
  if (!resp.ok) throw new Error(await errorText(resp));
  return resp.json();
}

function applyModelSettings(s) {
  const form = $('c-form');
  form.base_url.value = s.base_url;
  form.model.value = s.model;
  form.api_key.value = '';
  form.api_key.placeholder = s.has_key ? '已保存（留空则不改）' : '没有可以不填';
  for (const name of JOB_LIMITS) form[name].value = s[name];
}

async function saveModelSettings(event) {
  event.preventDefault();
  const form = $('c-form');
  const update = { base_url: form.base_url.value.trim(), model: form.model.value.trim() };
  if (form.api_key.value.trim()) update.api_key = form.api_key.value.trim();
  for (const name of JOB_LIMITS) update[name] = Number(form[name].value);
  $('c-setup-status').textContent = '保存中…';
  try {
    applyModelSettings(await modelFetch('/api/settings/caption', {
      method: 'PUT', headers: PAGE_HEADERS, body: JSON.stringify(update),
    }));
    $('c-setup-status').textContent = '已保存。可以点「测试」看看效果。';
  } catch (err) {
    $('c-setup-status').textContent = `保存失败：${err.message}`;
  }
}

async function testModelSettings() {
  $('c-setup-status').textContent = '测试中，模型在看一张红色圆形的示例图…';
  try {
    const r = await modelFetch('/api/settings/caption/test', { method: 'POST', headers: PAGE_HEADERS });
    $('c-setup-status').textContent = r.ok ? `成功（${r.seconds} 秒）：${r.text}` : `失败：${r.error}`;
  } catch (err) {
    $('c-setup-status').textContent = `测试失败：${err.message}`;
  }
}

// --- 语义搜索: the embeddings API behind meaning search ---

const IMAGE_FORMAT_LABELS = {
  none: '不发送图片（只用文件名、路径和描述）',
  input_object: 'input: [{"image": "data:…"}]（Jina 等）',
  input_string: 'input: ["data:image/…"]',
  messages: 'messages + image_url（vLLM 等，每次一张）',
};

function embedProgressText(p, imagesOn) {
  if (!p.cards) return '';
  const parts = [`文字向量 ${p.text}/${p.cards}`];
  if (imagesOn) parts.push(`图片向量 ${p.image}/${p.image_cards}`);
  if (p.rejected) parts.push(`被拒绝或无法读取 ${p.rejected}`);
  return `进度：${parts.join('，')}。新素材在每次扫描时补上。`;
}

function applyEmbedSettings(s) {
  const form = $('e-form');
  form.enabled.checked = s.enabled;
  form.base_url.value = s.base_url;
  form.model.value = s.model;
  form.api_key.value = '';
  form.api_key.placeholder = s.has_key ? '已保存（留空则不改）' : '没有可以不填';
  form.clear_key.checked = false;
  $('e-clear-key').hidden = !s.has_key;
  form.image_format.value = s.image_format;
  form.batch_size.value = s.batch_size;
  $('e-progress').textContent = s.active ? embedProgressText(s.progress, s.image_format !== 'none') : '';
}

function embedUpdate() {
  const form = $('e-form');
  const update = {
    enabled: form.enabled.checked,
    base_url: form.base_url.value.trim(),
    model: form.model.value.trim(),
    image_format: form.image_format.value,
    batch_size: Number(form.batch_size.value),
  };
  // Omitted = keep the stored key; "" = clear it (the server also clears it when the address changes).
  if (form.api_key.value.trim()) update.api_key = form.api_key.value.trim();
  else if (form.clear_key.checked) update.api_key = '';
  return update;
}

async function saveEmbedSettings(event) {
  event.preventDefault();
  $('e-status').textContent = '保存中…';
  try {
    applyEmbedSettings(await modelFetch('/api/settings/embedding', {
      method: 'PUT', headers: PAGE_HEADERS, body: JSON.stringify(embedUpdate()),
    }));
    $('e-status').textContent = '已保存。下一次扫描开始计算向量（可以点「扫描」马上开始）。';
  } catch (err) {
    $('e-status').textContent = `保存失败：${err.message}`;
  }
}

function probeLine(label, r) {
  if (!r) return `${label}：未测试（没选图片格式）`;
  return r.ok ? `${label}：成功，向量 ${r.dim} 维` : `${label}：失败，${r.error}`;
}

async function testEmbedSettings() {
  $('e-status').textContent = '测试已保存的设置…';
  try {
    const r = await modelFetch('/api/settings/embedding/test', { method: 'POST', headers: PAGE_HEADERS });
    $('e-status').textContent = `${probeLine('文字', r.text)}；${probeLine('图片', r.image)}`;
  } catch (err) {
    $('e-status').textContent = `测试失败：${err.message}`;
  }
}

// Fill both forms before showing them, so a late response can't wipe what the user typed.
async function openModelSettings() {
  $('c-setup-status').textContent = '';
  $('e-status').textContent = '';
  const [caption, embed] = await Promise.allSettled([
    modelFetch('/api/settings/caption'), modelFetch('/api/settings/embedding'),
  ]);
  if (caption.status === 'fulfilled') applyModelSettings(caption.value);
  else $('c-setup-status').textContent = `设置读取失败：${caption.reason.message}`;
  if (embed.status === 'fulfilled') applyEmbedSettings(embed.value);
  else $('e-status').textContent = `设置读取失败：${embed.reason.message}`;
  $('settings').showModal();
}

function initSettings() {
  $('e-form').image_format.replaceChildren(
    ...Object.entries(IMAGE_FORMAT_LABELS).map(([value, label]) => el('option', { value }, [label])),
  );
  $('settings-btn').addEventListener('click', openModelSettings);
  $('s-close').addEventListener('click', () => $('settings').close());
  $('c-form').addEventListener('submit', saveModelSettings);
  $('c-test').addEventListener('click', testModelSettings);
  $('e-form').addEventListener('submit', saveEmbedSettings);
  $('e-test').addEventListener('click', testEmbedSettings);
}

initSettings();
