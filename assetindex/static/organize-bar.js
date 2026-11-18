'use strict';

// "整理" row in the task bar: the organize analysis while it runs, else what awaits review.
// Loaded after captions.js ($, setText).

const ORGANIZE_POLL_MS = 3000;
const ORGANIZE_IDLE_POLL_MS = 15000;
// stopping: "停止" was pressed; the job finishes its current pack first, keep saying so.
const organizeBar = { timer: 0, busy: false, stopping: false };

function organizeText(data) {
  const job = data.job;
  if (job.running) {
    const phase = { units: '整理包列表', sampling: '抽样比对文件', hashing: '细比可能重复的文件', classifying: '让模型看图归类' }[job.phase] ?? '';
    return `正在分析 ${job.root}:/${job.prefix ?? ''}：${phase}${job.total ? ` ${job.done + job.failed}/${job.total}` : ''}`;
  }
  if (job.requested) return '已提交，等 captioner 开始…';
  const counts = Object.values(data.counts);
  if (!counts.length) return '还没分析过：打开整理页，找重复、给素材包归类';
  const sum = (key) => counts.reduce((n, c) => n + (c[key] ?? 0), 0);
  const parts = [`待确认 ${sum('proposed')}`, `重复 ${sum('duplicates')}`, `已确认 ${sum('accepted')}`];
  return (job.error ? `${job.error}；` : '') + parts.join('，');
}

async function refreshOrganizeBar() {
  clearTimeout(organizeBar.timer);
  try {
    const resp = await fetch('/api/organize');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    organizeBar.busy = data.job.running || data.job.requested;
    if (!organizeBar.busy) organizeBar.stopping = false;
    const row = $('task-organize');
    row.classList.toggle('busy', organizeBar.busy);
    row.classList.toggle('failed', !organizeBar.busy && Boolean(data.job.error));
    $('organize-task-stop').hidden = !organizeBar.busy || organizeBar.stopping;
    setTaskText($('organize-task-text'), organizeBar.stopping ? '正在停止：处理完手上这个包就停…' : organizeText(data));
  } catch (err) {
    setTaskText($('organize-task-text'), `状态读取失败：${err.message}`);
  }
  clearTimeout(organizeBar.timer);
  organizeBar.timer = setTimeout(refreshOrganizeBar, organizeBar.busy ? ORGANIZE_POLL_MS : ORGANIZE_IDLE_POLL_MS);
}

async function stopOrganize() {
  $('organize-task-stop').hidden = true;
  try {
    const resp = await fetch('/api/organize/stop', { method: 'POST', headers: { 'X-Requested-With': 'fetch' } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    organizeBar.stopping = true;
    setTaskText($('organize-task-text'), '正在停止：处理完手上这个包就停…');
  } catch (err) {
    $('organize-task-stop').hidden = false;
    setTaskText($('organize-task-text'), `没能停止：${err.message}`);
  }
}

$('organize-task-stop').addEventListener('click', stopOrganize);
refreshOrganizeBar();
