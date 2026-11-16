'use strict';

// "看图描述" dialog: a folder scope, start/stop and progress (the model is set in "设置").
// Loaded after app.js ($, el) and settings.js (PAGE_HEADERS, errorText).

const CAPTION_POLL_MS = 3000;
const CAPTION_IDLE_POLL_MS = 15000;  // a job can also start from another browser or the command line
const CAPTION_DEBOUNCE_MS = 300;
const CAPTION_PICKUP_WARN_MS = 30000;
const captionState = { timer: 0, countSeq: 0, folderSeq: 0, statusSeq: 0, debounce: 0, waitingSince: null, wasRunning: false };

// Status comes from polls and from start/stop; only the latest request may update the dialog.
async function fetchCaptionStatus(url, options) {
  const seq = ++captionState.statusSeq;
  const status = await captionFetch(url, options);
  if (seq === captionState.statusSeq) applyCaptionStatus(status);
}

async function captionFetch(url, options = {}) {
  const resp = await fetch(url, options);
  if (!resp.ok) throw new Error(await errorText(resp));
  return resp.json();
}

function captionScope() {
  const form = $('c-scope');
  return { root: form.root.value, prefix: form.prefix.value.trim(), redo: form.redo.checked };
}

function applyCaptionSettings(s) {
  const roots = $('c-scope').root;
  if (roots.options.length !== s.roots.length) {
    roots.replaceChildren(...s.roots.map((r) => el('option', { value: r }, [r])));
  }
  $('c-start').disabled = !s.active;
  $('c-need-model').hidden = s.active;
}

async function refreshCount() {
  const seq = ++captionState.countSeq;
  const scope = captionScope();
  if (!scope.root) return;
  const params = new URLSearchParams({ root: scope.root, prefix: scope.prefix, redo: scope.redo });
  try {
    const c = await captionFetch(`/api/captions/scope?${params}`);
    if (seq !== captionState.countSeq) return;
    $('c-count').textContent = c.cards
      ? `范围内有图的卡片 ${c.cards} 个，已有描述 ${c.done} 个，这次要处理 ${c.todo} 个。`
      : '这个范围里没有能看的图片（文件夹名要完整，可以从下拉里选）。';
  } catch (err) {
    if (seq === captionState.countSeq) $('c-count').textContent = `统计失败：${err.message}`;
  }
}

async function refreshFolders() {
  const seq = ++captionState.folderSeq;
  const scope = captionScope();
  if (!scope.root) return;
  // Suggest the children of the folder typed so far ("2D/Ic" -> children of "2D").
  const typed = scope.prefix.replace(/\\/g, '/');
  const parent = typed.endsWith('/') ? typed.slice(0, -1) : typed.slice(0, Math.max(0, typed.lastIndexOf('/')));
  const params = new URLSearchParams({ root: scope.root, prefix: parent });
  try {
    const { folders } = await captionFetch(`/api/captions/folders?${params}`);
    if (seq !== captionState.folderSeq) return;
    $('c-folders').replaceChildren(...folders.map((f) => el('option', { value: f })));
  } catch {
    // Suggestions are a convenience; typing the path still works.
  }
}

function scopeChanged() {
  clearTimeout(captionState.debounce);
  captionState.debounce = setTimeout(() => { refreshCount(); refreshFolders(); }, CAPTION_DEBOUNCE_MS);
}

function captionStatusText(s) {
  const where = s.root ? `${s.root}:/${s.prefix ?? ''}` : '';
  if (s.running) {
    const failed = s.failed ? `，失败 ${s.failed}` : '';
    return `正在描述 ${where}：${s.done + s.failed}/${s.total}${failed}${s.current ? `，当前 ${s.current}` : ''}`;
  }
  if (s.requested) {
    const waited = Date.now() - (captionState.waitingSince ?? Date.now());
    return waited > CAPTION_PICKUP_WARN_MS
      ? '已提交，但还没开始：检查 captioner 服务有没有在运行（docker compose ps）。'
      : '已提交，马上开始…';
  }
  if (!s.finished) return '';
  const when = new Date(s.finished * 1000).toLocaleString('zh-CN');
  const summary = `上次任务（${when}，${where}）：完成 ${s.done}/${s.total}${s.failed ? `，失败 ${s.failed}` : ''}`;
  if (s.error) return `${summary}。${s.error}`;
  return s.stopped ? `${summary}，已手动停止。` : `${summary}。`;
}

// Only touch text that changed: polls repeat it, and screen readers announce every write.
function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

function showProgress(bar, s) {
  bar.hidden = !s.running;
  bar.max = Math.max(1, s.total);
  bar.value = s.done + s.failed;
}

function applyCaptionStatus(s) {
  const busy = s.running || s.requested;
  captionState.waitingSince = s.requested && !s.running ? (captionState.waitingSince ?? Date.now()) : null;
  const text = captionStatusText(s);
  setText($('c-status'), text);
  $('c-stop').hidden = !busy;
  $('c-start').hidden = busy;
  showProgress($('c-bar'), s);
  // The task bar under the header shows the same, with the dialog closed.
  const row = $('task-caption');
  row.classList.toggle('busy', busy);
  row.classList.toggle('failed', !busy && Boolean(s.error));
  setText($('caption-task-text'), text || '空闲');
  $('caption-task-text').title = text;
  $('caption-task-stop').hidden = !busy;
  showProgress($('caption-task-bar'), s);
  if (captionState.wasRunning && !busy) {  // just finished: new numbers and new captions
    refreshCount();
    if (state.offset <= PAGE_SIZE) search();  // not when the user has paged further down
  }
  captionState.wasRunning = busy;
}

// Polls all the time (fast while a job runs) so the task bar is always current.
async function refreshCaptionStatus() {
  clearTimeout(captionState.timer);
  let busy = captionState.wasRunning;
  try {
    await fetchCaptionStatus('/api/captions');
    busy = captionState.wasRunning;
  } catch (err) {
    setText($('c-status'), `状态读取失败：${err.message}`);
    setText($('caption-task-text'), `状态读取失败：${err.message}`);
    $('task-caption').classList.remove('busy');
    $('task-caption').classList.add('failed');
    $('caption-task-stop').hidden = true;
    $('caption-task-bar').hidden = true;
  }
  clearTimeout(captionState.timer);  // a call that overlapped this one must not leave a second chain
  captionState.timer = setTimeout(refreshCaptionStatus, busy ? CAPTION_POLL_MS : CAPTION_IDLE_POLL_MS);
}

async function startCaptions(event) {
  event.preventDefault();
  try {
    await fetchCaptionStatus('/api/captions', {
      method: 'POST', headers: PAGE_HEADERS, body: JSON.stringify(captionScope()),
    });
  } catch (err) {
    $('c-status').textContent = `没能开始：${err.message}`;
  }
  refreshCaptionStatus();  // poll fast from now on
}

async function stopCaptions() {
  $('c-status').textContent = '正在停止（等当前这张图描述完）…';
  try {
    await fetchCaptionStatus('/api/captions/stop', { method: 'POST', headers: PAGE_HEADERS });
  } catch (err) {
    $('c-status').textContent = `没能停止：${err.message}`;
  }
}

async function openCaptions() {
  try {
    applyCaptionSettings(await captionFetch('/api/settings/caption'));
  } catch (err) {
    $('c-status').textContent = `设置读取失败：${err.message}`;
  }
  $('captions').showModal();
  refreshCount();
  refreshFolders();
  refreshCaptionStatus();
}

function initCaptions() {
  $('caption-task-open').addEventListener('click', openCaptions);
  $('caption-task-stop').addEventListener('click', stopCaptions);
  $('c-close').addEventListener('click', () => $('captions').close());
  $('c-scope').addEventListener('submit', startCaptions);
  $('c-scope').addEventListener('input', scopeChanged);
  $('c-stop').addEventListener('click', stopCaptions);
  refreshCaptionStatus();
}

initCaptions();
