'use strict';

// Organize page: run the analysis, review proposals (accept, reject, edit, split, keep).
// Names and paths come from the NAS and the model: always set text, never HTML.

const PAGE_SIZE = 50;
const POLL_BUSY_MS = 3000;
const POLL_IDLE_MS = 15000;
const HEADERS = { 'X-Requested-With': 'fetch', 'Content-Type': 'application/json' };
const TABS = [
  ['proposed', '待确认'], ['duplicates', '重复'], ['accepted', '已确认'],
  ['rejected', '已拒绝'], ['todo', '未分析'], ['failed', '失败'], ['all', '全部'],
];
const STATUS_LABELS = { todo: '未分析', proposed: '待确认', accepted: '已确认', rejected: '已拒绝' };
const PHASES = { units: '整理包列表', sampling: '抽样比对文件', hashing: '细比可能重复的文件', classifying: '让模型看图归类' };
const view = { root: '', show: 'proposed', category: '', q: '', offset: 0, seq: 0, statusSeq: 0, timer: 0, wasBusy: false };

const $ = (id) => document.getElementById(id);

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

function size(bytes) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

async function api(url, options = {}) {
  const resp = await fetch(url, options);
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try { detail = (await resp.json()).detail ?? detail; } catch { /* not JSON */ }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return resp.json();
}

const post = (url, body) => api(url, { method: 'POST', headers: HEADERS, body: JSON.stringify(body) });

function card(label, value, note) {
  return el('div', { className: 'org-card' }, [
    el('div', { className: 'org-card-value', text: value }),
    el('div', { className: 'org-card-label', text: label }),
    ...(note ? [el('div', { className: 'org-card-note', text: note })] : []),
  ]);
}

function renderSummary(data) {
  const counts = data.counts[view.root] ?? {};
  const report = data.report[view.root];
  const cards = [card('个包', String(counts.all ?? 0), report ? '' : '还没分析过')];
  if (report) {
    cards.push(card('组重复的包', String(report.dup_groups), `多出 ${report.dup_copies} 份，可省 ${size(report.dup_wasted)}`));
    cards.push(card('处跨包重复文件', String(report.files.groups), `可省 ${size(report.files.wasted)}，只提示，不建议移动`));
    cards.push(card('处包内重复文件', String(report.files.inner_groups), `${size(report.files.inner_wasted)}，多数是正常的（如 fbx 自带贴图）`));
  }
  cards.push(card('条待确认', String((counts.proposed ?? 0) + (counts.duplicates ?? 0)), `已确认 ${counts.accepted ?? 0}，已拒绝 ${counts.rejected ?? 0}`));
  $('o-summary').replaceChildren(...cards);
  $('o-tabs').replaceChildren(...TABS.map(([key, label]) => {
    const button = el('button', { type: 'button', className: key === view.show ? 'tab active' : 'tab', text: `${label} ${counts[key] ?? 0}` });
    button.addEventListener('click', () => { view.show = key; renderSummary(data); reload(); });
    return button;
  }));
}

function jobText(job) {
  const where = job.root ? `${job.root}:/${job.prefix ?? ''}` : '';
  if (job.running) {
    const phase = PHASES[job.phase] ?? '';
    const count = job.total ? `${job.done + job.failed}/${job.total}` : '';
    return `正在分析 ${where}：${phase} ${count}${job.failed ? `，失败 ${job.failed}` : ''}${job.current ? `，当前 ${job.current}` : ''}`;
  }
  if (job.requested) return '已提交，等 captioner 开始（它正在做看图描述的话，会等那个任务结束）…';
  if (!job.finished) return '';
  const when = new Date(job.finished * 1000).toLocaleString('zh-CN', { hour12: false });
  const summary = `上次分析（${when}，${where}）：归类 ${job.done}/${job.total}${job.failed ? `，失败 ${job.failed}` : ''}`;
  if (job.error) return `${summary}。${job.error}`;
  return job.stopped ? `${summary}，已手动停止。` : `${summary}。`;
}

function renderJob(data) {
  const job = data.job;
  const busy = job.running || job.requested;
  $('o-status').textContent = data.model_ready ? jobText(job) : '要先在主页「看图描述」里设置好看图模型的接口，整理会用同一个模型。';
  $('o-start').hidden = busy;
  $('o-start').disabled = !data.model_ready;
  $('o-stop').hidden = !busy;
  const bar = $('o-bar');
  bar.hidden = !job.running || !job.total;
  bar.max = Math.max(1, job.total);
  bar.value = job.done + job.failed;
  if (view.wasBusy && !busy) reload();  // finished: new proposals
  view.wasBusy = busy;
  return busy;
}

async function refresh() {
  clearTimeout(view.timer);
  const seq = ++view.statusSeq;
  let busy = view.wasBusy;
  try {
    const data = await api('/api/organize');
    if (seq !== view.statusSeq) return;  // a newer refresh is on its way (and will reschedule)
    if (!view.root) initRoots(data);
    renderSummary(data);
    busy = renderJob(data);
  } catch (err) {
    if (seq !== view.statusSeq) return;
    $('o-status').textContent = `状态读取失败：${err.message}`;
  }
  clearTimeout(view.timer);  // a call that overlapped this one must not leave a second chain
  view.timer = setTimeout(refresh, busy ? POLL_BUSY_MS : POLL_IDLE_MS);
}

function initRoots(data) {
  const select = $('o-form').root;
  select.replaceChildren(...data.roots.map((r) => el('option', { value: r }, [r])));
  view.root = data.roots[0] ?? '';
  $('o-cats').replaceChildren(...data.categories.map((c) => el('li', {}, [el('code', { text: c.path }), ` ${c.description}`])));
  $('o-category').append(...data.categories.map((c) => el('option', { value: c.path, text: c.path })),
    el('option', { value: '_Unsorted', text: '_Unsorted' }), el('option', { value: '_Duplicates', text: '_Duplicates' }));
  select.addEventListener('change', () => { view.root = select.value; refresh(); reload(); });
  reload();
}

function actionButton(label, handler, strong = false) {
  const button = el('button', { type: 'button', className: strong ? 'link-btn strong' : 'link-btn', text: label });
  button.addEventListener('click', async () => {
    button.disabled = true;
    try { await handler(); } catch (err) { alert(err.message); } finally { button.disabled = false; }
  });
  return button;
}

function targetEditor(item, holder) {
  const input = el('input', { type: 'text', className: 'org-target-input', value: item.target ?? '' });
  const save = actionButton('保存', async () => {
    await post('/api/organize/units/target', { root: view.root, path: item.path, target: input.value });
    afterChange();
  }, true);
  const cancel = actionButton('取消', async () => holder.replaceChildren(...targetView(item, holder)));
  holder.replaceChildren(input, save, cancel);
  input.focus();
}

function targetView(item, holder) {
  if (!item.target) return [el('span', { className: 'muted', text: item.error ? `失败：${item.error}` : '还没分析' })];
  return [
    el('span', { className: 'org-target', text: item.target }),
    actionButton('改', async () => targetEditor(item, holder)),
  ];
}

function row(item) {
  const name = item.path.split('/').pop();
  const dir = item.path.slice(0, item.path.length - name.length);
  const check = el('input', { type: 'checkbox', className: 'org-check', 'aria-label': `选中 ${item.path}` });
  check.dataset.path = item.path;
  const thumb = item.thumb
    ? el('img', { src: item.thumb, alt: '', loading: 'lazy', className: 'org-thumb' })
    : el('div', { className: 'org-thumb empty', text: item.is_dir ? '📁' : '📄' });
  const target = el('div', { className: 'org-target-line' });
  target.replaceChildren(...targetView(item, target));
  const actions = [];
  if (item.target && item.status !== 'accepted') actions.push(actionButton('确认', () => decide([item.path], 'accept'), true));
  if (item.target && item.status !== 'rejected') actions.push(actionButton('拒绝', () => decide([item.path], 'reject')));
  if (item.status === 'accepted' || item.status === 'rejected') actions.push(actionButton('撤回', () => decide([item.path], 'reset')));
  if (item.dup_of) actions.push(actionButton('改为保留这份', async () => { await post('/api/organize/units/keep', { root: view.root, path: item.path }); afterChange(); }));
  if (item.is_dir) actions.push(actionButton('拆开', async () => { await post('/api/organize/split', { root: view.root, path: item.path }); afterChange(); }));
  const parent = dir.replace(/\/$/, '');
  if (parent.includes('/')) {
    actions.push(actionButton('合并', async () => {
      if (!window.confirm(`把「${parent}」整个当成一个包？里面分开的包会合在一起，它们的建议作废。`)) return;
      await post('/api/organize/join', { root: view.root, path: parent });
      afterChange();
    }));
  }
  return el('li', { className: `org-row status-${item.status}` }, [
    check,
    thumb,
    el('div', { className: 'org-main' }, [
      el('div', { className: 'org-from' }, [
        el('span', { className: 'muted', text: dir }), el('strong', { text: name }),
        el('span', { className: 'muted', text: ` · ${item.is_dir ? `${item.files} 个文件 · ` : ''}${size(item.size)}` }),
      ]),
      el('div', { className: 'org-to' }, [el('span', { className: 'arrow', text: '→' }), target]),
      ...(item.reason ? [el('div', { className: 'org-reason', text: item.reason })] : []),
    ]),
    el('div', { className: 'org-side' }, [
      el('span', { className: `org-badge ${item.dup_of ? 'dup' : item.status}`, text: item.dup_of ? '重复' : STATUS_LABELS[item.status] }),
      el('div', { className: 'org-actions' }, actions),
    ]),
  ]);
}

async function load(reset) {
  if (!view.root) return;
  if (reset) view.offset = 0;
  const seq = ++view.seq;
  const params = new URLSearchParams({ root: view.root, show: view.show, category: view.category, q: view.q, limit: PAGE_SIZE, offset: view.offset });
  try {
    const data = await api(`/api/organize/units?${params}`);
    if (seq !== view.seq) return;
    const rows = data.items.map(row);
    if (reset) $('o-list').replaceChildren(...rows); else $('o-list').append(...rows);
    view.offset += data.items.length;
    $('o-count').textContent = `${data.total} 个`;
    $('o-more').hidden = view.offset >= data.total;
    $('o-all').checked = false;
  } catch (err) {
    if (seq === view.seq) $('o-count').textContent = `读取失败：${err.message}`;
  }
}

const reload = () => load(true);

function afterChange() {
  refresh();
  reload();
}

const DECIDE_BATCH = 500;  // the server takes at most this many paths per request

async function decide(paths, action) {
  if (!paths.length) return;
  for (let i = 0; i < paths.length; i += DECIDE_BATCH) {
    await post('/api/organize/units/decide', { root: view.root, paths: paths.slice(i, i + DECIDE_BATCH), action });
  }
  afterChange();
}

function selectedPaths() {
  return [...document.querySelectorAll('.org-check:checked')].map((c) => c.dataset.path);
}

function init() {
  $('o-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = $('o-form');
    try {
      await post('/api/organize', { root: form.root.value, prefix: form.prefix.value.trim(), redo: form.redo.checked });
    } catch (err) {
      $('o-status').textContent = `没能开始：${err.message}`;
    }
    refresh();
  });
  $('o-stop').addEventListener('click', async () => {
    $('o-status').textContent = '正在停止（等当前这个包问完）…';
    try { await post('/api/organize/stop', {}); } catch (err) { $('o-status').textContent = `没能停止：${err.message}`; }
    refresh();
  });
  $('o-category').addEventListener('change', () => { view.category = $('o-category').value; reload(); });
  let timer;
  $('o-q').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => { view.q = $('o-q').value.trim(); reload(); }, 300); });
  $('o-all').addEventListener('change', () => {
    document.querySelectorAll('.org-check').forEach((c) => { c.checked = $('o-all').checked; });
  });
  $('o-accept').addEventListener('click', () => decide(selectedPaths(), 'accept').catch((err) => alert(err.message)));
  $('o-reject').addEventListener('click', () => decide(selectedPaths(), 'reject').catch((err) => alert(err.message)));
  $('o-more').addEventListener('click', () => load(false));
  refresh();
}

init();
