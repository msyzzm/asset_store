'use strict';

// "更新" row in the task bar: shows the running version and what's new upstream, and asks
// the host's updater (deploy/updater) to check or to pull + rebuild. Loaded after
// captions.js ($, setText, PAGE_HEADERS, errorText).

const UPDATE_POLL_MS = 3000;
const UPDATE_IDLE_POLL_MS = 60000;
const updateState = { timer: 0, seq: 0, busy: false, loadedVersion: null, sawUpdate: false };

function shortTime(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleString('zh-CN', { hour12: false, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function updateText(s) {
  if (!s.installed) return '没装更新服务（只需装一次，见 docs/deploy.md「网页一键更新」）';
  if (s.state === 'running') return '正在更新：拉代码、重建镜像，网页会断开一会儿，完成后自动刷新…';
  if (s.state === 'checking' || s.requested === 'check') return '正在检查有没有新版本…';
  if (s.requested === 'update') return '已提交，马上开始更新…';
  const rebuild = s.needs_rebuild ? `；服务还在跑 ${s.deployed}，代码已是 ${s.current}，点更新重新构建` : '';
  if (s.state === 'error') return (s.error || '出错了') + rebuild;
  const version = runningVersion(s) ? `当前 ${runningVersion(s)}` : '';
  if (s.behind > 0) return `${version}，有 ${s.behind} 个新提交：${s.pending[0] ?? ''}`;
  if (s.needs_rebuild) return `代码已更新到 ${s.current}，但服务还在跑 ${s.deployed}（上次构建没成功？），点更新重新构建`;
  const when = s.checked ? `，检查于 ${shortTime(s.checked)}` : '';
  return version ? `已是最新（${runningVersion(s)}${when}）` : '还没检查过';
}

// What the containers run: the last successful build, not just what was pulled.
function runningVersion(s) {
  return s.deployed || s.current || '';
}

function applyUpdateState(s) {
  const busy = Boolean(s.requested) || s.state === 'running' || s.state === 'checking';
  if (updateState.loadedVersion === null) updateState.loadedVersion = runningVersion(s);  // once, at page load
  if (s.state === 'running' || s.requested === 'update') updateState.sawUpdate = true;
  // New code is live: reload so the page runs it too.
  if (updateState.sawUpdate && s.state === 'done' && runningVersion(s) !== updateState.loadedVersion) {
    location.reload();
    return;
  }
  if (!s.requested && (s.state === 'done' || s.state === 'error')) updateState.sawUpdate = false;  // over
  const row = $('task-update');
  const actionable = s.behind > 0 || s.needs_rebuild;
  row.classList.toggle('busy', busy);
  row.classList.toggle('failed', s.state === 'error');
  row.classList.toggle('attention', !busy && actionable);
  setText($('update-task-text'), updateText(s));
  $('update-task-text').title = s.state === 'error' ? (s.log ?? '') : s.pending.join('\n');
  $('update-check').hidden = !s.installed || busy;
  $('update-run').hidden = !s.installed || busy || !actionable;
  updateState.busy = busy;
}

async function refreshUpdate() {
  clearTimeout(updateState.timer);
  const seq = ++updateState.seq;
  try {
    const resp = await fetch('/api/update');
    if (!resp.ok) throw new Error(await errorText(resp));
    const s = await resp.json();
    if (seq === updateState.seq) applyUpdateState(s);
  } catch (err) {
    if (seq === updateState.seq) {
      // During an update the web container restarts: expected, keep polling.
      setText($('update-task-text'), updateState.sawUpdate ? '服务重启中…' : `状态读取失败：${err.message}`);
    }
  }
  clearTimeout(updateState.timer);
  updateState.timer = setTimeout(refreshUpdate, updateState.busy || updateState.sawUpdate ? UPDATE_POLL_MS : UPDATE_IDLE_POLL_MS);
}

async function requestUpdate(action) {
  if (action === 'update' && !confirm('更新会拉取最新代码并重建、重启服务。正在跑的扫描或看图描述会中断，重启后可以接着做。继续？')) return;
  try {
    const resp = await fetch('/api/update', { method: 'POST', headers: PAGE_HEADERS, body: JSON.stringify({ action }) });
    if (!resp.ok) throw new Error(await errorText(resp));
    applyUpdateState(await resp.json());
  } catch (err) {
    setText($('update-task-text'), `没能提交：${err.message}`);
  }
  refreshUpdate();
}

function initUpdate() {
  $('update-check').addEventListener('click', () => requestUpdate('check'));
  $('update-run').addEventListener('click', () => requestUpdate('update'));
  refreshUpdate();
}

initUpdate();
