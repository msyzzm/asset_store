'use strict';

// "设置" dialog: the vision model that "看图描述" and "素材整理" both use. Loaded after app.js.

const PAGE_HEADERS = { 'X-Requested-With': 'fetch', 'Content-Type': 'application/json' };

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
}

async function saveModelSettings(event) {
  event.preventDefault();
  const form = $('c-form');
  const update = { base_url: form.base_url.value.trim(), model: form.model.value.trim() };
  if (form.api_key.value.trim()) update.api_key = form.api_key.value.trim();
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

// Fill the form before showing it, so a late response can't wipe what the user typed.
async function openModelSettings() {
  $('c-setup-status').textContent = '';
  try {
    applyModelSettings(await modelFetch('/api/settings/caption'));
  } catch (err) {
    $('c-setup-status').textContent = `设置读取失败：${err.message}`;
  }
  $('settings').showModal();
}

function initSettings() {
  $('settings-btn').addEventListener('click', openModelSettings);
  $('s-close').addEventListener('click', () => $('settings').close());
  $('c-form').addEventListener('submit', saveModelSettings);
  $('c-test').addEventListener('click', testModelSettings);
}

initSettings();
