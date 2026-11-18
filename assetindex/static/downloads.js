'use strict';

// "下载" row in the task bar and its dialog: links the fetcher service downloads with aria2
// into the library's _下载 folder. Loaded after app.js (formatSize), settings.js
// (PAGE_HEADERS, modelFetch), captions.js (setText) and files-bar.js (getJson, setBar, finishedAt).

const DL_POLL_MS = 2000;
const DL_IDLE_POLL_MS = 15000;
const DL_STATUS = {
  queued: '排队中', active: '下载中', uploading: '上传中', done: '完成', duplicate: '完成，库里已有一样的',
  failed: '失败', cancelled: '已取消',
};
const dl = { timer: 0 };

function dlName(item) {
  return item.title || item.path?.split('/').pop() || item.url;
}

function dlProgressText(p) {
  if (!p?.total) return p?.done ? `已下 ${formatSize(p.done)}` : '连接中…';
  const pct = Math.floor((p.done / p.total) * 100);
  return `${pct}%，${formatSize(p.done)}/${formatSize(p.total)}${p.speed ? `，${formatSize(p.speed)}/s` : ''}`;
}

function dlLastText(items, idle) {
  const last = items.find((i) => i.finished);
  if (!last) return idle;
  return `上次（${finishedAt(last.finished)}）：${dlName(last)} ${DL_STATUS[last.status]}`;
}

function dlRowText(state, items) {
  if (!state.mount) return '下载服务没在运行（更新一次后会自动启动）';
  if (!Object.values(state.mount.roots).some(Boolean)) return 'NAS 对下载服务是只读的（.env 里设 ASSET_MOVER_ACCESS=rw）';
  const active = items.filter((i) => i.status === 'active');
  const queued = state.counts.queued ?? 0;
  if (active.length) {
    const first = active[0];
    const more = active.length + queued - 1;
    return `正在下载 ${dlName(first)}：${dlProgressText(first.progress)}${more > 0 ? `，还有 ${more} 个` : ''}`;
  }
  if (queued) return `${queued} 个排队中…`;
  return dlLastText(items, '空闲');
}

// Uploads are taken by the web server itself, no fetcher needed
function upRowText(items) {
  const uploading = items.filter((i) => i.status === 'uploading');
  if (uploading.length) {
    const more = uploading.length > 1 ? `，还有 ${uploading.length - 1} 个` : '';
    return `正在上传 ${dlName(uploading[0])}：${dlProgressText(uploading[0].progress)}${more}`;
  }
  return dlLastText(items, '空闲（也可以把文件、文件夹直接拖到页面上）');
}

function dlButton(label, action, id) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'link-btn';
  button.textContent = label;
  button.dataset.action = action;
  button.dataset.id = id;
  return button;
}

function dlItem(item) {
  const li = document.createElement('li');
  li.className = `dl-item dl-${item.status}`;
  const head = document.createElement('div');
  head.className = 'dl-head';
  const name = document.createElement('span');
  name.className = 'dl-name';
  name.textContent = dlName(item);
  name.title = item.source === 'upload' ? '从你的电脑上传' : item.url;
  const status = document.createElement('span');
  status.className = 'dl-state';
  const running = item.status === 'active' || item.status === 'uploading';
  status.textContent = running ? dlProgressText(item.progress) : DL_STATUS[item.status];
  head.append(name, status);
  if (running || item.status === 'queued') head.append(dlButton('取消', 'cancel', item.id));
  if ((item.status === 'failed' || item.status === 'cancelled') && item.source !== 'upload') {
    head.append(dlButton('重试', 'retry', item.id));
  }
  li.append(head);
  if (running) {
    const bar = document.createElement('progress');
    bar.className = 'task-bar dl-bar';
    bar.max = Math.max(1, item.progress?.total ?? 0);
    bar.value = item.progress?.done ?? 0;
    li.append(bar);
  }
  const where = item.gone ? `已从 ${item.path} 移走或删除` : `在 ${item.path}`;  // gone: organized away since
  const note = item.error || (item.dup_of ? `和 ${item.dup_of} 一模一样` : item.path ? where : '');
  if (note) {
    const p = document.createElement('p');
    p.className = 'dl-note';
    p.textContent = note;
    li.append(p);
  }
  return li;
}

// One task bar row ("downloads" or "uploads"); whether something is running in it
function dlRenderRow(prefix, items, text, running) {
  const busy = items.some((i) => ['active', 'queued', 'uploading'].includes(i.status));
  const row = $(`task-${prefix}`);
  row.classList.toggle('busy', busy);
  row.classList.toggle('failed', !busy && items[0]?.status === 'failed');
  setTaskText($(`${prefix}-task-text`), text);
  const shown = items.filter((i) => i.status === running && i.progress?.total);
  setBar($(`${prefix}-task-bar`), shown.length > 0,
    shown.reduce((n, i) => n + i.progress.done, 0), shown.reduce((n, i) => n + i.progress.total, 0));
  return busy;
}

function renderDownloads(state) {
  const downs = state.items.filter((i) => i.source !== 'upload');
  const ups = state.items.filter((i) => i.source === 'upload');
  const busy = [
    dlRenderRow('downloads', downs, dlRowText(state, downs), 'active'),
    dlRenderRow('uploads', ups, upRowText(ups), 'uploading'),
  ];
  if ($('downloads').open) $('d-list').replaceChildren(...downs.map(dlItem));
  if ($('uploads').open) $('u-list').replaceChildren(...ups.map(dlItem));
  return busy.some(Boolean);
}

async function refreshDownloads() {
  clearTimeout(dl.timer);
  let busy = false;
  try {
    busy = renderDownloads(await getJson('/api/downloads'));
  } catch (err) {
    setTaskText($('downloads-task-text'), `状态读取失败：${err.message}`);
    setTaskText($('uploads-task-text'), `状态读取失败：${err.message}`);
  }
  clearTimeout(dl.timer);  // another refresh may have run while this one waited
  const watched = busy || $('downloads').open || $('uploads').open;
  dl.timer = setTimeout(refreshDownloads, watched ? DL_POLL_MS : DL_IDLE_POLL_MS);
}

async function dlPost(url, body) {
  const options = { method: 'POST', headers: PAGE_HEADERS };
  return modelFetch(url, body === undefined ? options : { ...options, body: JSON.stringify(body) });
}

$('downloads-open').addEventListener('click', () => {
  $('downloads').showModal();
  refreshDownloads();
});
$('d-close').addEventListener('click', () => $('downloads').close());

$('d-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('button[type=submit]');
  const url = form.url.value.trim();
  const title = form.title.value.trim();
  submit.disabled = true;
  try {
    await dlPost('/api/downloads', { url, ...(title ? { title } : {}) });
    // Cleared only if still what was sent: a next link typed meanwhile stays.
    if (form.url.value.trim() === url) form.reset();
    setText($('d-status'), '已加入下载队列。');
  } catch (err) {
    setText($('d-status'), err.message);
  } finally {
    submit.disabled = false;
  }
  refreshDownloads();
});

for (const [list, status] of [['d-list', 'd-status'], ['u-list', 'u-status']]) {
  $(list).addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    try {
      await dlPost(`/api/downloads/${button.dataset.id}/${button.dataset.action}`);
      setText($(status), '');
    } catch (err) {
      setText($(status), err.message);
    }
    refreshDownloads();
  });
}

refreshDownloads();
