'use strict';

// Semantic search settings dialog and the "按描述搜" toggle. Loaded after app.js.

const IMAGE_FORMAT_LABELS = {
  none: '不发送图片（只用文件名和路径）',
  input_object: 'input: [{"image": "data:…"}]（Jina 等）',
  input_string: 'input: ["data:image/…"]',
  messages: 'messages + image_url（vLLM 等，每次一张）',
};
const PAGE_HEADERS = { 'X-Requested-With': 'fetch', 'Content-Type': 'application/json' };

async function errorText(resp) {
  try {
    const body = await resp.json();
    if (typeof body.detail === 'string') return body.detail;
  } catch { /* not JSON */ }
  return `HTTP ${resp.status}`;
}

function progressText(p, imagesOn) {
  if (!p.cards) return '';
  const parts = [`文字向量 ${p.text}/${p.cards}`];
  if (imagesOn) parts.push(`图片向量 ${p.image}/${p.image_cards}`);
  if (p.rejected) parts.push(`被拒绝或无法读取 ${p.rejected}`);
  return `进度：${parts.join('，')}。新素材在每次扫描时补上。`;
}

function applySettings(s) {
  const form = $('s-form');
  form.enabled.checked = s.enabled;
  form.base_url.value = s.base_url;
  form.model.value = s.model;
  form.api_key.value = '';
  form.api_key.placeholder = s.has_key ? '已保存（留空则不改）' : '没有可以不填';
  form.clear_key.checked = false;
  $('s-clear-key').hidden = !s.has_key;
  form.image_format.value = s.image_format;
  form.batch_size.value = s.batch_size;
  $('s-progress').textContent = s.active ? progressText(s.progress, s.image_format !== 'none') : '';
  $('semantic-toggle').hidden = !s.active;
  if (!s.active && state.mode === 'semantic') {
    // Switched off (or a ?mode=semantic link with nothing set up): show plain results instead.
    $('semantic').checked = false;
    state.mode = 'text';
    search();
  }
}

async function loadSettings() {
  try {
    const resp = await fetch('/api/settings/embedding');
    if (!resp.ok) throw new Error(await errorText(resp));
    applySettings(await resp.json());
  } catch (err) {
    $('s-status').textContent = `设置读取失败：${err.message}`;
  }
}

function formUpdate() {
  const form = $('s-form');
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

async function saveSettings(event) {
  event.preventDefault();
  $('s-status').textContent = '保存中…';
  try {
    const resp = await fetch('/api/settings/embedding', {
      method: 'PUT', headers: PAGE_HEADERS, body: JSON.stringify(formUpdate()),
    });
    if (!resp.ok) throw new Error(await errorText(resp));
    applySettings(await resp.json());
    $('s-status').textContent = '已保存。下一次扫描开始计算向量（可以点「扫描」马上开始）。';
  } catch (err) {
    $('s-status').textContent = `保存失败：${err.message}`;
  }
}

function probeLine(label, r) {
  if (!r) return `${label}：未测试（没选图片格式）`;
  return r.ok ? `${label}：成功，向量 ${r.dim} 维` : `${label}：失败，${r.error}`;
}

async function testSettings() {
  $('s-status').textContent = '测试已保存的设置…';
  try {
    const resp = await fetch('/api/settings/embedding/test', { method: 'POST', headers: PAGE_HEADERS });
    if (!resp.ok) throw new Error(await errorText(resp));
    const result = await resp.json();
    $('s-status').textContent = `${probeLine('文字', result.text)}；${probeLine('图片', result.image)}`;
  } catch (err) {
    $('s-status').textContent = `测试失败：${err.message}`;
  }
}

function initSettings() {
  const select = $('s-form').image_format;
  select.replaceChildren(...Object.entries(IMAGE_FORMAT_LABELS).map(([value, label]) => el('option', { value }, [label])));
  // Fill the form before showing it, so a late response can't wipe what the user typed.
  $('settings-btn').addEventListener('click', async () => {
    $('s-status').textContent = '';
    await loadSettings();
    $('settings').showModal();
  });
  $('s-close').addEventListener('click', () => $('settings').close());
  $('s-form').addEventListener('submit', saveSettings);
  $('s-test').addEventListener('click', testSettings);
  $('semantic').checked = state.mode === 'semantic';
  $('semantic').addEventListener('change', () => {
    state.mode = $('semantic').checked ? 'semantic' : 'text';
    search();
  });
  loadSettings();
}

initSettings();
