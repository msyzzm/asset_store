'use strict';

// 外部素材 page: search the free asset sites (assetindex/external_web.py) through the server, look at an asset,
// queue one of its downloads (the server asks the site for the link).

const HEADERS = { 'X-Requested-With': 'fetch', 'Content-Type': 'application/json' };
const $ = (id) => document.getElementById(id);
const state = { q: '', sources: {}, seq: 0 };  // per source: {items, result}; seq: the latest search

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'text') node.textContent = value;
    else node[key] = value;
  }
  node.append(...children);
  return node;
}

// Same as app.js (not loaded on this page).
function formatSize(bytes) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

async function api(url, options = {}) {
  const resp = await fetch(url, options);
  let body = null;
  try { body = await resp.json(); } catch { /* not JSON */ }
  if (!resp.ok) throw new Error(typeof body?.detail === 'string' ? body.detail : `HTTP ${resp.status}`);
  return body;
}

function chosenSources() {
  return [...document.querySelectorAll('input[name=source]:checked')].map((box) => box.value);
}

function sourceLabel(name) {
  return document.querySelector(`input[name=source][value="${name}"]`)?.parentElement.textContent.trim() || name;
}

function mark(item) {
  if (item.in_library) return el('span', { className: 'x-mark', text: '已有', title: `库里有：${item.in_library}` });
  if (item.downloaded) return el('span', { className: 'x-mark queued', text: '已下载' });
  return null;
}

function itemCard(item) {
  const img = el('img', { src: item.thumb ?? '', loading: 'lazy', alt: '', className: 'x-photo', referrerPolicy: 'no-referrer' });
  img.addEventListener('error', () => img.replaceWith(el('span', { className: 'ext', text: item.kind || '?' })), { once: true });
  const thumb = el('div', { className: 'thumb' }, item.kind ? [img, el('span', { className: 'x-kind', text: item.kind })] : [img]);
  const badge = mark(item);
  if (badge) thumb.append(badge);
  const node = el('button', { className: 'card', type: 'button', title: item.title }, [
    thumb,
    el('div', { className: 'name', text: item.title }),
    el('div', { className: 'path', text: item.tags.join(' · ') }),
  ]);
  node.addEventListener('click', () => openDetail(item.source, item.id));
  return node;
}

function renderSource(name, result) {
  const kept = state.sources[name];
  const head = el('div', { className: 'x-source-head' }, [
    el('h2', { text: result.label }),
    el('span', { className: 'x-count', text: result.error ? '' : [result.total == null ? '' : `共 ${result.total} 个`, result.license].filter(Boolean).join(' · ') }),
    el('a', { href: result.credit, target: '_blank', rel: 'noopener noreferrer', text: `素材来自 ${result.label} ↗` }),
  ]);
  const section = el('section', { className: 'x-source', id: `x-${name}` }, [head]);
  if (result.error) section.append(el('p', { className: 'x-error', text: `${result.label} 没搜到：${result.error}` }));
  if (result.notice) section.append(el('p', { className: 'x-notice', text: result.notice }));
  else if (!kept.items.length) section.append(el('p', { className: 'x-empty', text: '没有结果，换个英文词试试。' }));
  section.append(el('div', { className: 'grid' }, kept.items.map(itemCard)));
  const left = result.total == null ? null : result.total - kept.items.length;
  if (left > 0 || (left == null && result.more)) {
    const more = el('button', { className: 'more', type: 'button', text: left ? `加载更多（还有 ${left} 个）` : '加载更多' });
    more.addEventListener('click', () => loadMore(name, more));
    section.append(more);
  }
  return section;
}

async function search(event) {
  event?.preventDefault();
  const seq = ++state.seq;
  const q = $('q').value.trim();
  const names = chosenSources();
  if (!names.length) {
    $('x-results').replaceChildren(el('p', { className: 'x-empty', text: '至少勾选一个网站。' }));
    return;
  }
  state.q = q;
  state.sources = {};
  $('x-hint').hidden = true;
  $('x-results').replaceChildren(...names.map((name) => el('section', { className: 'x-source', id: `x-${name}` }, [
    el('p', { className: 'x-empty', text: `${sourceLabel(name)} 搜索中…` })])));
  // Each site shows as soon as it answers: OpenGameArt can take 10 s or more.
  await Promise.all(names.map(async (name) => {
    const body = await api(`/api/external/search?${new URLSearchParams({ q, source: name })}`)
      .catch((err) => ({ chinese: false, sources: { [name]: { label: sourceLabel(name), credit: '', license: '', error: err.message, items: [], total: 0 } } }));
    if (seq !== state.seq) return;  // a newer search started meanwhile
    const result = body.sources[name];
    state.sources[name] = { items: result.items, result };
    if (body.chinese) $('x-hint').hidden = false;
    $(`x-${name}`).replaceWith(renderSource(name, result));
  }));
}

async function loadMore(name, button) {
  const kept = state.sources[name];
  const seq = state.seq;
  button.disabled = true;
  try {
    const offset = kept.result.next ?? kept.items.length;
    const body = await api(`/api/external/search?${new URLSearchParams({ q: state.q, source: name, offset })}`);
    if (seq !== state.seq) return;  // the page shows another search now
    const result = body.sources[name];
    if (result.error) throw new Error(result.error);  // keep what is shown, and the button to try again
    // an empty page: the end, whatever the total said (rows skipped, or a site that stops serving)
    const shown = result.items.length ? result : { ...kept.result, total: kept.items.length, more: false };
    kept.items = [...kept.items, ...result.items];
    kept.result = shown;
    $(`x-${name}`).replaceWith(renderSource(name, shown));
  } catch (err) {
    button.disabled = false;
    button.textContent = `加载失败：${err.message}（点这里重试）`;
  }
}

// ---- one asset ----

function metaRow(term, value) {
  return value ? [el('dt', { text: term }), el('dd', {}, [value])] : [];
}

function optionRow(source, id, option) {
  const row = el('li', {}, [
    el('span', { className: 'xv-label', text: option.label }),
    el('span', { className: 'xv-size', text: option.size ? formatSize(option.size) : '' }),
  ]);
  if (!option.ready) {
    row.append(el('span', { className: 'xv-later', text: '多个文件，下一步支持' }));
    return row;
  }
  const button = el('button', { className: 'btn', type: 'button', text: '下载' });
  button.addEventListener('click', () => queue(source, id, option, button));
  row.append(button);
  return row;
}

async function queue(source, id, option, button) {
  button.disabled = true;
  try {
    await api(`/api/external/${source}/${encodeURIComponent(id)}/download`, {
      method: 'POST', headers: HEADERS, body: JSON.stringify({ option: option.key }),
    });
    button.textContent = '已加入';
    $('xv-status').textContent = `${option.label} 已加入下载队列，下完会出现在素材库的 _下载 里（主页任务栏「下载」看进度）。`;
  } catch (err) {
    button.disabled = false;
    $('xv-status').textContent = err.message;
  }
}

async function openDetail(source, id) {
  $('xv-title').textContent = '加载中…';
  $('xv-preview').replaceChildren();
  $('xv-meta').replaceChildren();
  $('xv-options').replaceChildren();
  $('xv-status').textContent = '';
  $('x-viewer').showModal();
  let d;
  try {
    d = await api(`/api/external/${source}/${encodeURIComponent(id)}`);
  } catch (err) {
    $('xv-title').textContent = '打不开';
    $('xv-status').textContent = err.message;
    return;
  }
  $('xv-title').textContent = d.title;
  $('xv-preview').replaceChildren(...d.previews.map((src) => el('img', { src, alt: d.title, referrerPolicy: 'no-referrer' })));
  const page = el('a', { href: d.page, target: '_blank', rel: 'noopener noreferrer', text: `${d.page} ↗` });
  $('xv-meta').replaceChildren(
    ...metaRow('类型', d.kind),
    ...metaRow('授权', d.license && (d.license.startsWith('CC0') ? `${d.license}（可商用，不用署名）` : `${d.license}（用前看原页面的授权和署名要求）`)),
    ...metaRow('作者', d.authors),
    ...metaRow('署名', d.attribution),
    ...metaRow('说明', d.description),
    ...metaRow('标签', d.tags.join('、')),
    ...metaRow('库里已有', d.in_library),
    ...metaRow('原页面', page),
  );
  if (d.downloaded) $('xv-status').textContent = '这个素材已经在下载队列里或下载过了。';
  const options = d.options.map((o) => optionRow(source, d.id, o));
  if (d.note || !options.length) options.push(el('li', { className: 'xv-note', text: d.note || '网站没给可下载的文件。' }));
  $('xv-options').replaceChildren(...options);
}

// ---- the keys some sites need: one card each ----

const KEY_SITES = [
  {
    site: 'sketchfab', name: 'Sketchfab', field: 'API token', use: '下载模型时用（搜索不用）。',
    how: ['登录 sketchfab.com', '右上角头像 → Settings', 'Password & API → 复制 API Token'],
    link: 'https://sketchfab.com/settings/password',
  },
  {
    site: 'freesound', name: 'Freesound', field: 'API key', use: '搜索和下载音效都要用。',
    how: ['登录 freesound.org', '打开申请页，填表提交', '复制 “Client secret/Api key” 那一栏'],
    link: 'https://freesound.org/apiv2/apply',
  },
  {
    site: 'polypizza', name: 'Poly Pizza', field: 'API key', use: '搜索和下载模型都要用。',
    how: ['登录 poly.pizza', '打开 API 设置页，新建一个应用', '复制它的 API key'],
    link: 'https://poly.pizza/settings/api',
  },
];

function keyCard(info, isSet) {
  const input = el('input', { name: 'key', type: 'password', autocomplete: 'new-password',
    placeholder: isSet ? '粘贴新的 key 替换（留空不改）' : `粘贴 ${info.name} 的 ${info.field}` });
  const status = el('p', { className: 'xk-msg', role: 'status' });
  const save = el('button', { className: 'btn', type: 'submit', text: '保存' });
  const actions = el('div', { className: 'xk-actions' }, [save]);
  if (isSet) {
    const clear = el('button', { className: 'btn secondary', type: 'button', text: '清除' });
    clear.addEventListener('click', () => saveKey(info, '', status, [save, clear]));
    actions.append(clear);
  }
  const form = el('form', { className: 'xk-form' }, [
    el('label', { className: 'xk-field' }, [el('span', { text: info.field }), input]),
    actions,
  ]);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (!input.value.trim()) { status.textContent = '先粘贴 key 再保存。'; return; }
    saveKey(info, input.value.trim(), status, [save]);
  });
  return el('section', { className: `xk-site${isSet ? ' is-set' : ''}`, ariaLabel: info.name }, [
    el('header', { className: 'xk-head' }, [
      el('h3', { text: info.name }),
      el('span', { className: 'xk-badge', text: isSet ? '已填' : '未填' }),
      el('a', { href: info.link, target: '_blank', rel: 'noopener noreferrer', text: '去拿 key ↗' }),
    ]),
    el('p', { className: 'xk-use', text: info.use }),
    el('ol', { className: 'xk-how' }, info.how.map((step) => el('li', { text: step }))),
    form,
    status,
  ]);
}

function renderKeys(set) {
  $('xk-sites').replaceChildren(...KEY_SITES.map((info) => keyCard(info, Boolean(set[info.site]))));
}

async function showKeys() {
  $('xk-status').textContent = '';
  $('xk-sites').replaceChildren(el('p', { className: 'hint', text: '读取中…' }));
  $('x-keys').showModal();
  try {
    renderKeys(await api('/api/external/keys'));
  } catch (err) {
    $('xk-sites').replaceChildren();
    $('xk-status').textContent = err.message;
  }
}

async function saveKey(info, key, status, buttons) {
  for (const b of buttons) b.disabled = true;
  try {
    const set = await api('/api/external/keys', { method: 'PUT', headers: HEADERS, body: JSON.stringify({ [info.site]: key }) });
    renderKeys(set);
    $('xk-status').textContent = `${info.name} 的 key 已${key ? '保存' : '清除'}。`;
    $('q').value = state.q;  // the search shown, again, now with the keys
    search();
  } catch (err) {
    for (const b of buttons) b.disabled = false;
    status.textContent = err.message;
  }
}

$('x-keys-btn').addEventListener('click', showKeys);
$('xk-close').addEventListener('click', () => $('x-keys').close());
$('x-form').addEventListener('submit', search);
$('xv-close').addEventListener('click', () => $('x-viewer').close());
for (const box of document.querySelectorAll('input[name=source]')) box.addEventListener('change', () => search());
search();
