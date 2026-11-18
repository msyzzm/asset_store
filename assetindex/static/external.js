'use strict';

// 外部素材 page: search Poly Haven and ambientCG through the server, look at an asset,
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

function mark(item) {
  if (item.in_library) return el('span', { className: 'x-mark', text: '已有', title: `库里有：${item.in_library}` });
  if (item.downloaded) return el('span', { className: 'x-mark queued', text: '已下载' });
  return null;
}

function itemCard(item) {
  const img = el('img', { src: item.thumb ?? '', loading: 'lazy', alt: '', className: 'x-photo', referrerPolicy: 'no-referrer' });
  img.addEventListener('error', () => img.replaceWith(el('span', { className: 'ext', text: item.kind || '?' })), { once: true });
  const thumb = el('div', { className: 'thumb' }, [img, el('span', { className: 'x-kind', text: item.kind })]);
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
    el('span', { className: 'x-count', text: result.error ? '' : `共 ${result.total} 个 · CC0` }),
    el('a', { href: result.credit, target: '_blank', rel: 'noopener noreferrer', text: `素材来自 ${result.label} ↗` }),
  ]);
  const section = el('section', { className: 'x-source', id: `x-${name}` }, [head]);
  if (result.error) section.append(el('p', { className: 'x-error', text: `${result.label} 没搜到：${result.error}` }));
  else if (!kept.items.length) section.append(el('p', { className: 'x-empty', text: '没有结果，换个英文词试试。' }));
  section.append(el('div', { className: 'grid' }, kept.items.map(itemCard)));
  if (kept.items.length < result.total) {
    const more = el('button', { className: 'more', type: 'button', text: `加载更多（还有 ${result.total - kept.items.length} 个）` });
    more.addEventListener('click', () => loadMore(name, more));
    section.append(more);
  }
  return section;
}

async function search(event) {
  event?.preventDefault();
  const seq = ++state.seq;
  const q = $('q').value.trim();
  $('x-results').replaceChildren(el('p', { className: 'x-empty', text: '搜索中…' }));
  const results = await Promise.all(chosenSources().map((name) => api(`/api/external/search?${new URLSearchParams({ q, source: name })}`)
    .catch((err) => ({ chinese: false, sources: { [name]: { label: name, credit: '', error: err.message, items: [], total: 0 } } }))));
  if (seq !== state.seq) return;  // a newer search started meanwhile
  state.q = q;
  const sources = Object.assign({}, ...results.map((r) => r.sources));
  state.sources = Object.fromEntries(Object.entries(sources).map(([n, r]) => [n, { items: r.items, result: r }]));
  $('x-hint').hidden = !results.some((r) => r.chinese);
  $('x-results').replaceChildren(...Object.entries(sources).map(([name, r]) => renderSource(name, r)));
}

async function loadMore(name, button) {
  const kept = state.sources[name];
  const seq = state.seq;
  button.disabled = true;
  try {
    const body = await api(`/api/external/search?${new URLSearchParams({ q: state.q, source: name, offset: kept.items.length })}`);
    if (seq !== state.seq) return;  // the page shows another search now
    const result = body.sources[name];
    kept.items = [...kept.items, ...result.items];
    kept.result = result;
    $(`x-${name}`).replaceWith(renderSource(name, result));
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
    ...metaRow('授权', `${d.license}（可商用，不用署名）`),
    ...metaRow('作者', d.authors),
    ...metaRow('说明', d.description),
    ...metaRow('标签', d.tags.join('、')),
    ...metaRow('库里已有', d.in_library),
    ...metaRow('原页面', page),
  );
  if (d.downloaded) $('xv-status').textContent = '这个素材已经在下载队列里或下载过了。';
  const options = d.options.length ? d.options.map((o) => optionRow(source, d.id, o)) : [el('li', { text: '网站没给可下载的文件。' })];
  $('xv-options').replaceChildren(...options);
}

$('x-form').addEventListener('submit', search);
$('xv-close').addEventListener('click', () => $('x-viewer').close());
for (const box of document.querySelectorAll('input[name=source]')) box.addEventListener('change', () => search());
search();
