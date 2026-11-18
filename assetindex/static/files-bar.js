'use strict';

// "文件操作" and "打标签" rows in the task bar: what the mover is doing to the NAS (moving,
// unpacking, repairing, checking, deleting), and the tagging job. Loaded after captions.js
// ($, setText).

const FILES_POLL_MS = 3000;
const FILES_IDLE_POLL_MS = 15000;
const MOVE_ACTIONS = {
  apply: '移动', undo: '撤销移动', recover: '收尾上次中断的移动', unpack: '解包',
  verify: '校验压缩包', purge: '永久删除', repair: '修复损坏的包', lift: '拆外层压缩包',
};
const filesBar = { timer: 0 };

function finishedAt(epoch) {
  return new Date(epoch * 1000).toLocaleString('zh-CN', { hour12: false });
}

function setBar(bar, running, done, total) {
  bar.hidden = !running || !total;
  bar.max = Math.max(1, total);
  bar.value = done;
}

function moveText(job) {
  const what = MOVE_ACTIONS[job.action] ?? '移动';
  if (job.running) return job.total ? `正在${what} ${job.done}/${job.total}…` : `正在${what}…`;
  if (job.requested) return job.waiting ? `${what}已提交，等扫描或整理分析结束后开始…` : `${what}已提交，马上开始…`;
  if (!job.finished) return '空闲';
  const failed = job.problems?.length ? `，${job.problems.length} 个没成（原因在整理页）` : '';
  const ending = job.error ? `。${job.error}` : job.stopped ? '，已手动停止' : '';
  return `上次（${finishedAt(job.finished)}）：${what}，成功 ${job.moved}/${job.total}${failed}${ending}`;
}

function tagText(s) {
  if (s.running) return `正在打标签 ${s.done + s.failed}/${s.total}${s.failed ? `，失败 ${s.failed}` : ''}…`;
  if (s.requested) return '已提交，等看图模型空出来…';
  const ending = s.error ? `。${s.error}` : s.stopped ? '，已手动停止' : '';
  return `上次（${finishedAt(s.finished)}）：${s.done} 个包打好标签${s.failed ? `，${s.failed} 个失败` : ''}${ending}`;
}

async function getJson(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function refreshMoveRow() {
  try {
    const job = (await getJson('/api/organize/moves')).job;
    const busy = job.running || job.requested;
    const row = $('task-files');
    row.classList.toggle('busy', busy);
    row.classList.toggle('failed', !busy && Boolean(job.error));
    setTaskText($('files-task-text'), moveText(job));
    setBar($('files-task-bar'), job.running, job.done, job.total);
    return busy;
  } catch (err) {
    setTaskText($('files-task-text'), `状态读取失败：${err.message}`);
    return false;
  }
}

async function refreshTagRow() {
  try {
    const s = await getJson('/api/organize/tags');
    const busy = s.running || s.requested;
    const row = $('task-tags');
    row.hidden = !busy && !s.finished;  // never run: no row
    row.classList.toggle('busy', busy);
    row.classList.toggle('failed', !busy && Boolean(s.error));
    setTaskText($('tags-task-text'), tagText(s));
    setBar($('tags-task-bar'), s.running, s.done + s.failed, s.total);
    return busy;
  } catch (err) {
    setTaskText($('tags-task-text'), `状态读取失败：${err.message}`);
    return false;
  }
}

async function refreshFilesBar() {
  clearTimeout(filesBar.timer);
  const [moving, tagging] = await Promise.all([refreshMoveRow(), refreshTagRow()]);
  filesBar.timer = setTimeout(refreshFilesBar, moving || tagging ? FILES_POLL_MS : FILES_IDLE_POLL_MS);
}

refreshFilesBar();
