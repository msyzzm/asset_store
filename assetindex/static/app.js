'use strict';

const PAGE_SIZE = 60;
const DEBOUNCE_MS = 300;
const BROWSER_IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg']);
const THUMB_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'tga', 'psd', 'tif', 'tiff']);
// Files whose thumbnail the server can render: images, plus .uasset (embedded editor thumbnail).
const PREVIEW_EXTS = new Set([...THUMB_EXTS, 'uasset']);
const KIND_LABELS = {
  image: '图片', audio: '音频', video: '视频', model: '模型',
  archive: '压缩包', unreal: 'Unreal', other: '其他',
};
// Hover delay: sweeping the mouse across the grid must not start dozens of thumbnail renders.
const HOVER_DELAY_MS = 150;
const HOVER_FPS = 12;
const MAX_HOVER_FRAMES = 24;
const PLAY_FPS = 12;
const MAX_PLAY_FRAMES = 120;
const ENTRY_PAGE_SIZE = 100;
const ENGINE_LABELS = { unreal: 'UE', unity: 'Unity' };
const MODEL_EXTS = new Set(['fbx']);
// Parsing runs on the main thread; bigger files would freeze the tab for many seconds.
const MAX_MODEL_BYTES = 64 * 1024 * 1024;
const SCAN_POLL_MS = 5000;
// The scanner starts a requested scan within its 60 s minimum gap plus a poll.
const SCAN_START_TIMEOUT_MS = 2 * 60 * 1000;

const $ = (id) => document.getElementById(id);
const state = { q: '', kind: '', mode: 'text', offset: 0, requestSeq: 0, viewerSeq: 0, stopPlayer: null, stopModel: null, scanBusy: false, scanTimer: 0, scanSeq: 0, scanWaitingSince: null };
const frameCache = new Map();
// Only one card plays at a time; a re-rendered grid never fires mouseleave, so stop it explicitly.
let stopHover = null;

// Filenames are untrusted: always set text via textContent, never innerHTML.
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'text') node.textContent = value;
    else if (key === 'className') node.className = value;
    else node.setAttribute(key, value);
  }
  node.append(...children);
  return node;
}

function formatSize(bytes) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function dirname(relPath) {
  const cut = relPath.lastIndexOf('/');
  return cut < 0 ? '' : relPath.slice(0, cut);
}

// mtime in the URL: thumbnails are cached for a week, so a changed file needs a new URL.
function thumbUrl(item) {
  return `/api/files/${item.id}/thumb?v=${Math.trunc(item.mtime)}`;
}

function extBadge(item) {
  return el('span', { className: 'ext', text: item.ext || '?' });
}

function thumbFor(item) {
  // Archives show a contact sheet built at scan time (sheet === 1), if any.
  const hasThumb = item.kind === 'archive' ? item.sheet === 1 : PREVIEW_EXTS.has(item.ext);
  if (!hasThumb) return extBadge(item);
  const img = el('img', { src: thumbUrl(item), loading: 'lazy', alt: '' });
  img.addEventListener('error', () => img.replaceWith(extBadge(item)), { once: true });
  return img;
}

function isSequence(item) {
  return item.frames > 1;
}

function sample(list, max) {
  if (list.length <= max) return list;
  return Array.from({ length: max }, (_, i) => list[Math.floor((i * list.length) / max)]);
}

function fetchFrames(id) {
  if (!frameCache.has(id)) {
    const pending = fetch(`/api/files/${id}/frames`)
      .then((resp) => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return resp.json();
      })
      .then((body) => body.frames);
    pending.catch(() => frameCache.delete(id));
    frameCache.set(id, pending);
  }
  return frameCache.get(id);
}

// Cycles through urls, skipping frames not loaded yet so playback never flashes blank.
// Returns a stop function that also cancels pending downloads.
function startFlipbook(urls, fps, onFrame, startAt = 0) {
  const ready = new Array(urls.length).fill(false);
  const loaders = urls.map((url, i) => {
    const pre = new Image();
    pre.onload = () => { ready[i] = true; };
    pre.src = url;
    return pre;
  });
  let current = startAt - 1;
  const timer = setInterval(() => {
    for (let step = 1; step <= urls.length; step += 1) {
      const next = (current + step) % urls.length;
      if (ready[next]) { current = next; onFrame(next); return; }
    }
  }, 1000 / fps);
  return () => {
    clearInterval(timer);
    loaders.forEach((pre, i) => { if (!ready[i]) pre.src = ''; });
  };
}

function attachHoverPlayback(node, img, item) {
  let token = 0;
  let delay = null;
  const stop = () => {
    token += 1;
    clearTimeout(delay);
    stopFlipbook?.();
    stopFlipbook = null;
    img.src = thumbUrl(item);
  };
  let stopFlipbook = null;
  node.addEventListener('mouseenter', () => {
    stopHover?.();
    stopHover = stop;
    const mine = ++token;
    delay = setTimeout(async () => {
      let frames;
      try { frames = await fetchFrames(item.id); } catch { return; }
      if (mine !== token) return; // mouse left (or re-entered) while frames were loading
      const urls = sample(frames, MAX_HOVER_FRAMES).map(thumbUrl);
      stopFlipbook = startFlipbook(urls, HOVER_FPS, (k) => { img.src = urls[k]; });
    }, HOVER_DELAY_MS);
  });
  node.addEventListener('mouseleave', () => {
    stop();
    if (stopHover === stop) stopHover = null;
  });
}

function engineLabel(item) {
  const name = ENGINE_LABELS[item.engine];
  if (!name) return '';
  return item.engine_version ? `${name} ${item.engine_version}` : name;
}

function basename(path) {
  return path.slice(path.lastIndexOf('/') + 1);
}

function sizeLine(item) {
  const parts = [KIND_LABELS[item.kind] ?? item.kind, formatSize(item.size)];
  if (item.entry_count) parts.push(`${item.entry_count.toLocaleString('zh-CN')} 个文件`);
  return parts.join(' · ');
}

function card(item) {
  const thumb = thumbFor(item);
  const thumbBox = el('div', { className: 'thumb' }, [thumb]);
  if (isSequence(item)) thumbBox.append(el('span', { className: 'seq-badge', text: `▶ ${item.frames} 张` }));
  const engine = engineLabel(item);
  if (engine) thumbBox.append(el('span', { className: `engine-badge ${item.engine}`, text: engine }));
  const hits = (item.hits ?? []).map((hit) => el('div', { className: 'hit', title: hit, text: `包内：${basename(hit)}` }));
  const node = el('button', { className: 'card', type: 'button', title: item.rel_path }, [
    thumbBox,
    el('div', { className: 'name', text: item.name }),
    el('div', { className: 'path', text: `${item.root}:/${dirname(item.rel_path)}` }),
    el('div', { className: 'size', text: sizeLine(item) }),
    ...hits,
  ]);
  node.addEventListener('click', () => openViewer(item));
  if (isSequence(item) && thumb.tagName === 'IMG') attachHoverPlayback(node, thumb, item);
  return node;
}

async function load(reset) {
  const seq = ++state.requestSeq;
  const offset = reset ? 0 : state.offset;
  const params = new URLSearchParams({ q: state.q, limit: PAGE_SIZE, offset });
  if (state.kind) params.set('kind', state.kind);
  if (state.mode === 'semantic') params.set('mode', 'semantic');

  let body;
  try {
    const resp = await fetch(`/api/search?${params}`);
    if (!resp.ok) {
      const detail = await resp.json().then((b) => b.detail, () => null);
      throw new Error(typeof detail === 'string' ? detail : `HTTP ${resp.status}`);
    }
    body = await resp.json();
  } catch (err) {
    if (seq === state.requestSeq) $('summary').textContent = `搜索失败：${err.message}`;
    return;
  }
  if (seq !== state.requestSeq) return; // a newer search superseded this one

  if (reset) {
    stopHover?.();
    stopHover = null;
    $('grid').replaceChildren();
  }
  $('grid').append(...body.items.map(card));
  state.offset = offset + body.items.length;
  const semantic = state.mode === 'semantic' && state.q;
  $('summary').textContent = !body.total ? '没有找到匹配的素材'
    : semantic ? `按描述排序，最相关的 ${body.total} 个` : `共 ${body.total} 个结果`;
  $('more').hidden = state.offset >= body.total;
}

function syncUrl() {
  const params = new URLSearchParams();
  if (state.q) params.set('q', state.q);
  if (state.kind) params.set('kind', state.kind);
  if (state.mode === 'semantic') params.set('mode', 'semantic');
  const query = params.toString();
  history.replaceState(null, '', query ? `?${query}` : location.pathname);
}

function search() {
  state.q = $('q').value.trim();
  state.kind = $('kind').value;
  // Hide "more" until the new results land, or a click would page the new query onto the old grid.
  state.offset = 0;
  $('more').hidden = true;
  syncUrl();
  load(true);
}

function previewFor(item) {
  const raw = `/api/files/${item.id}/raw`;
  if (item.kind === 'image' && BROWSER_IMAGE_EXTS.has(item.ext)) return el('img', { src: raw, alt: item.name });
  if (item.kind === 'image' && THUMB_EXTS.has(item.ext)) return el('img', { src: thumbUrl(item), alt: item.name });
  if (item.kind === 'audio') return el('audio', { src: raw, controls: '', preload: 'metadata' });
  if (item.kind === 'video') return el('video', { src: raw, controls: '', preload: 'metadata' });
  if (item.kind === 'archive') return archivePanel(item);
  if (MODEL_EXTS.has(item.ext)) return modelPanel(item);
  return el('p', { className: 'muted', text: '该类型暂不支持预览' });
}

function stopModel() {
  state.stopModel?.();
  state.stopModel = null;
}

// three.js (~700 KB) is fetched on first use only.
function modelPanel(item) {
  if (item.size > MAX_MODEL_BYTES) {
    return el('p', { className: 'muted', text: `模型超过 ${formatSize(MAX_MODEL_BYTES)}，请下载后查看` });
  }
  const stage = el('div', { className: 'model-stage' });
  const status = el('span', { className: 'muted model-status', text: '加载 3D 预览…' });
  const toolbar = el('div', { className: 'model-toolbar' });
  const hint = el('span', { className: 'muted', text: '左键旋转 · 右键平移 · 滚轮缩放' });
  const onStatus = (text, isError = false) => {
    status.textContent = text;
    status.classList.toggle('error', isError);
  };
  const viewerSeq = state.viewerSeq;
  import('./model-viewer.js')
    .then(({ mountModelViewer }) => {
      if (viewerSeq !== state.viewerSeq) return; // dialog closed or another file opened meanwhile
      state.stopModel = mountModelViewer(stage, `/api/files/${item.id}/raw`, onStatus, toolbar, MAX_MODEL_BYTES);
    })
    .catch((err) => onStatus(`3D 预览加载失败：${err.message}`, true));
  return el('div', { className: 'model-panel' }, [stage, el('div', { className: 'model-bar' }, [toolbar, status, hint])]);
}

function archiveSummary(archive, total, filtered) {
  if (!archive) return '包内容尚未索引，下次扫描后可见';
  if (archive.status === 'error') return `无法读取包内容：${archive.error ?? '未知错误'}`;
  const parts = [];
  const engine = engineLabel(archive);
  if (engine) parts.push(archive.engine_version ? `${engine}（版本按目录名推断）` : engine);
  parts.push(filtered ? `匹配 ${total} / ${archive.entry_count} 个文件` : `${archive.entry_count} 个文件`);
  if (archive.truncated) parts.push('文件过多，只索引了一部分');
  return parts.join(' · ');
}

function entryIcon(entry) {
  return el('span', { className: 'entry-icon', text: entry.ext || '' });
}

// unitypackage: Unity's own asset preview; zip/rar/7z: the image itself, extracted on demand.
function entryThumb(item, entry) {
  const hasThumb = item.ext === 'unitypackage' ? entry.has_preview : PREVIEW_EXTS.has(entry.ext);
  if (!hasThumb) return entryIcon(entry);
  const img = el('img', {
    className: 'entry-thumb',
    loading: 'lazy',
    alt: '',
    src: `/api/files/${item.id}/entries/${entry.id}/thumb?v=${Math.trunc(item.mtime)}`,
  });
  img.addEventListener('error', () => img.replaceWith(entryIcon(entry)), { once: true });
  return img;
}

function entryRow(item, entry) {
  return el('li', {}, [
    entryThumb(item, entry),
    el('span', { className: 'entry-path', title: entry.path, text: entry.path }),
    el('span', { className: 'muted', text: formatSize(entry.size) }),
  ]);
}

// Contents of an archive, filterable and paged. Prefilled with the search query when
// the card was found through its contents.
function archivePanel(item) {
  const filter = el('input', { type: 'search', className: 'entry-filter', placeholder: '筛选包内文件' });
  const summary = el('p', { className: 'muted entry-summary' });
  const list = el('ul', { className: 'entries' });
  const more = el('button', { className: 'btn secondary', type: 'button', text: '加载更多' });
  more.hidden = true;
  // Only terms the archive's own path doesn't contain: those matched its contents.
  const path = item.rel_path.toLowerCase();
  const inner = state.q.split(/\s+/).filter((t) => t && !path.includes(t.toLowerCase()));
  filter.value = item.hits?.length ? inner.join(' ') : '';
  const viewerSeq = state.viewerSeq;
  let offset = 0;
  let requestSeq = 0;

  async function loadEntries(reset) {
    const seq = ++requestSeq;
    const start = reset ? 0 : offset;
    const params = new URLSearchParams({ q: filter.value.trim(), limit: ENTRY_PAGE_SIZE, offset: start });
    let body;
    try {
      const resp = await fetch(`/api/files/${item.id}/entries?${params}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      body = await resp.json();
    } catch (err) {
      if (seq === requestSeq) summary.textContent = `包内容加载失败：${err.message}`;
      return;
    }
    if (seq !== requestSeq || viewerSeq !== state.viewerSeq) return;
    if (reset) list.replaceChildren();
    list.append(...body.entries.map((entry) => entryRow(item, entry)));
    offset = start + body.entries.length;
    summary.textContent = archiveSummary(body.archive, body.total, Boolean(filter.value.trim()));
    more.hidden = offset >= body.total;
  }

  let timer;
  filter.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => loadEntries(true), DEBOUNCE_MS); });
  more.addEventListener('click', () => loadEntries(false));
  loadEntries(true);
  return el('div', { className: 'archive-panel' }, [filter, summary, list, more]);
}

function metaRows(item) {
  const rows = [
    ['位置', `${item.root}:/${item.rel_path}`],
    ['类型', `${KIND_LABELS[item.kind] ?? item.kind}（.${item.ext}）`],
    ['大小', formatSize(item.size)],
    ['修改时间', new Date(item.mtime * 1000).toLocaleString('zh-CN')],
  ];
  if (isSequence(item)) rows.push(['序列', `${item.frames} 张（拖动滑块逐张查看，下载当前张）`]);
  return rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]);
}

function setCurrentFile(file) {
  $('v-download').href = `/api/files/${file.id}/download`;
  $('v-copy').onclick = () => copyPath(`${file.root}:/${file.rel_path}`);
}

function stopPlayer() {
  state.stopPlayer?.();
  state.stopPlayer = null;
}

function frameUrl(file) {
  return BROWSER_IMAGE_EXTS.has(file.ext) ? `/api/files/${file.id}/raw` : thumbUrl(file);
}

function sequencePlayer(frames) {
  const img = el('img', { alt: '' });
  const toggle = el('button', { className: 'btn secondary', type: 'button' });
  const slider = el('input', {
    type: 'range', min: '0', max: String(frames.length - 1), value: '0', 'aria-label': '帧',
  });
  const label = el('span', { className: 'muted frame-label' });
  const playable = sample(frames.map((_, i) => i), MAX_PLAY_FRAMES);
  let currentIndex = 0;

  const show = (i) => {
    const file = frames[i];
    currentIndex = i;
    img.src = frameUrl(file);
    slider.value = String(i);
    label.textContent = `${i + 1} / ${frames.length} · ${file.name}`;
    setCurrentFile(file);
  };
  const pause = () => { stopPlayer(); toggle.textContent = '播放'; };
  const play = () => {
    stopPlayer();
    const urls = playable.map((i) => frameUrl(frames[i]));
    // Resume from the frame the slider is on instead of jumping back to the start.
    const resumeAt = Math.max(0, playable.findIndex((i) => i >= currentIndex));
    state.stopPlayer = startFlipbook(urls, PLAY_FPS, (k) => show(playable[k]), resumeAt);
    toggle.textContent = '暂停';
  };
  toggle.addEventListener('click', () => (state.stopPlayer ? pause() : play()));
  slider.addEventListener('input', () => { pause(); show(Number(slider.value)); });

  show(0);
  play();
  return el('div', { className: 'player' }, [
    img,
    el('div', { className: 'player-controls' }, [toggle, slider, label]),
  ]);
}

async function openViewer(item) {
  const seq = ++state.viewerSeq;
  stopPlayer();
  stopModel();
  $('v-name').textContent = item.name;
  $('v-preview').replaceChildren(previewFor(item));
  $('v-meta').replaceChildren(...metaRows(item));
  setCurrentFile(item);
  $('v-copy').textContent = '复制路径';
  $('viewer').showModal();
  if (!isSequence(item)) return;

  let frames;
  try {
    frames = await fetchFrames(item.id);
  } catch {
    return; // keep the single-image preview
  }
  if (seq !== state.viewerSeq || !$('viewer').open) return;
  $('v-preview').replaceChildren(sequencePlayer(frames));
}

async function copyPath(text) {
  try {
    // Clipboard API needs a secure context (https or localhost).
    await navigator.clipboard.writeText(text);
    $('v-copy').textContent = '已复制';
  } catch {
    $('v-copy').textContent = '复制失败，请手动选中上方路径';
  }
}

async function loadStats() {
  try {
    const resp = await fetch('/api/stats');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const stats = await resp.json();
    $('stats').textContent = Object.entries(stats)
      .sort((a, b) => b[1] - a[1])
      .map(([kind, n]) => `${KIND_LABELS[kind] ?? kind} ${n.toLocaleString('zh-CN')}`)
      .join(' · ');
  } catch (err) {
    $('stats').textContent = `统计加载失败：${err.message}`;
  }
}

function formatTime(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleString('zh-CN', { hour12: false });
}

function scanText(status) {
  if (status.running) return `正在扫描（${formatTime(status.started)} 开始）…`;
  if (status.requested) return '已请求扫描，扫描器一分钟内会开始…';
  if (status.finished == null) {
    return status.ok === false ? '上次扫描中途断了（扫描器被强制退出？看扫描日志）' : '';
  }
  const problem = status.ok === false ? '，有问题，看扫描日志' : '';
  return `上次扫描：${formatTime(status.finished)} 完成${problem}`;
}

// Polls while a scan is pending or running; when it ends, reloads stats (and results,
// unless the user has paged further down).
async function refreshScan(status) {
  clearTimeout(state.scanTimer);
  const seq = ++state.scanSeq;
  if (!status) {
    try {
      const resp = await fetch('/api/scan');
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      status = await resp.json();
    } catch (err) {
      if (seq !== state.scanSeq) return;
      $('scan-status').textContent = `扫描状态未知：${err.message}`;
      if (state.scanBusy) state.scanTimer = setTimeout(refreshScan, SCAN_POLL_MS);
      return;
    }
    if (seq !== state.scanSeq) return; // a newer refresh (e.g. the button) superseded this one
  }
  const waiting = status.requested && !status.running;
  state.scanWaitingSince = waiting ? (state.scanWaitingSince ?? Date.now()) : null;
  if (waiting && Date.now() - state.scanWaitingSince > SCAN_START_TIMEOUT_MS) {
    state.scanBusy = false;
    $('scan').disabled = false;
    $('scan-status').textContent = '扫描器没有响应：scanner 容器可能没在运行';
    return;
  }
  const busy = status.requested || status.running;
  if (state.scanBusy && !busy) {
    loadStats();
    if (state.offset <= PAGE_SIZE) search();
  }
  state.scanBusy = busy;
  $('scan').disabled = busy;
  $('scan-status').textContent = scanText(status);
  if (busy) state.scanTimer = setTimeout(refreshScan, SCAN_POLL_MS);
}

async function requestScan() {
  $('scan').disabled = true;
  state.scanWaitingSince = null;
  try {
    // The header proves the request comes from this page (see api_scan_request).
    const resp = await fetch('/api/scan', { method: 'POST', headers: { 'X-Requested-With': 'fetch' } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    await refreshScan(await resp.json());
  } catch (err) {
    $('scan').disabled = false;
    $('scan-status').textContent = `请求扫描失败：${err.message}`;
  }
}

function init() {
  const params = new URLSearchParams(location.search);
  $('q').value = params.get('q') ?? '';
  $('kind').value = params.get('kind') ?? '';
  state.mode = params.get('mode') === 'semantic' ? 'semantic' : 'text';

  let timer;
  $('q').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(search, DEBOUNCE_MS); });
  $('kind').addEventListener('change', search);
  $('more').addEventListener('click', () => load(false));
  $('v-close').addEventListener('click', () => $('viewer').close());
  // Clearing the preview stops audio/video playback when the dialog closes.
  $('viewer').addEventListener('close', () => {
    state.viewerSeq += 1; // drop a frames response still in flight
    stopPlayer();
    stopModel();
    $('v-preview').replaceChildren();
  });

  $('scan').addEventListener('click', requestScan);

  loadStats();
  search();
  refreshScan();
}

init();
