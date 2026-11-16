'use strict';

// Organize page, "执行整理": the move plan, moving the confirmed packs, undoing a run.
// Uses api, post, el and view from organize.js. Paths come from the NAS: text only.

const move = { timer: 0, seq: 0, plan: null, state: null, wasBusy: false };

function when(epoch) {
  return epoch ? new Date(epoch * 1000).toLocaleString('zh-CN', { hour12: false }) : '';
}

function mountText(mount) {
  if (mount == null) return '移动要用服务器上新加的 mover 服务：更新一次后它会自动启动。';
  if (!mount[view.root]) {
    return 'NAS 现在对 mover 是只读的，只能看移动计划。建议都确认完后，在服务器的 .env 里设 ASSET_MOVER_ACCESS=rw，再运行 docker compose up -d。';
  }
  return 'mover 可以写 NAS：确认过的包可以移动了。每次移动都有记录，可以撤销。';
}

function moveJobText(job) {
  if (job.running) {
    const what = job.action === 'undo' ? '正在撤销' : job.action === 'recover' ? '正在收尾上次中断的移动' : '正在移动';
    return job.total ? `${what} ${job.done}/${job.total}…` : `${what}…`;
  }
  if (job.requested) return job.waiting ? '已提交，等扫描或整理分析结束后开始…' : '已提交，mover 马上开始…';
  if (!job.finished) return '';
  const what = job.action === 'undo' ? `撤销了 ${job.moved} 个包` : job.action === 'recover' ? '收尾完成' : `移动了 ${job.moved} 个包`;
  const problems = job.problems.length ? `，${job.problems.length} 个没动（原因见下）` : '';
  const ending = job.error ? `。${job.error}` : job.stopped ? '，已手动停止。' : '。';
  return `上次（${when(job.finished)}）：${what}${problems}${ending}`;
}

function problemList(problems) {
  return problems.map((p) => el('li', {}, [el('span', { className: 'org-move-path', text: p.path }), el('span', { className: 'muted', text: ` ${p.reason}` })]));
}

function renderMoveState(data) {
  move.state = data;
  const job = data.job;
  const busy = job.running || job.requested;
  const writable = Boolean(data.mount?.[view.root]);
  $('m-mount').textContent = mountText(data.mount);
  $('m-status').textContent = moveJobText(job);
  $('m-job-problems').replaceChildren(...problemList(job.problems ?? []));
  $('m-stop').hidden = !busy || job.action === 'recover';  // settling cannot be stopped halfway
  $('m-start').hidden = busy;
  $('m-start').disabled = !writable || !move.plan?.moves;
  $('m-recover').hidden = !data.unsettled || busy;
  const bar = $('m-bar');
  bar.hidden = !job.running || !job.total;
  bar.max = Math.max(1, job.total);
  bar.value = job.done;
  $('m-history').replaceChildren(...data.history.map((run) => historyRow(run, writable && !busy)));
  $('m-history-empty').hidden = data.history.length > 0;
  return busy;
}

function historyRow(run, canAct) {
  const undone = run.moved - run.still_moved;
  const text = `${when(run.started)}，${run.root}：移动了 ${run.moved} 个包${undone ? `，已撤销 ${undone} 个` : ''}${run.open ? `，${run.open} 个中断了没收尾` : ''}`;
  const children = [el('span', { text })];
  if (run.still_moved > 0 && canAct && run.root === view.root) {
    const undo = el('button', { type: 'button', className: 'link-btn', text: '撤销这次' });
    undo.addEventListener('click', () => startMove({ action: 'undo', root: view.root, journal: run.journal },
      `把这次移动的 ${run.still_moved} 个包放回原来的位置？放回后它们的建议回到「待确认」。`));
    children.push(undo);
  }
  return el('li', {}, children);
}

async function refreshMoves() {
  clearTimeout(move.timer);
  const seq = ++move.seq;
  let busy = false;
  try {
    const data = await api('/api/organize/moves');
    if (seq !== move.seq) return;
    const finished = move.wasBusy && !(data.job.running || data.job.requested);
    if (finished) { move.plan = null; $('m-plan-view').hidden = true; }  // things moved: the plan is old
    busy = renderMoveState(data);
    if (finished) afterChange();
    move.wasBusy = busy;
  } catch (err) {
    if (seq !== move.seq) return;
    $('m-status').textContent = `移动状态读取失败：${err.message}`;
  }
  clearTimeout(move.timer);
  move.timer = setTimeout(refreshMoves, busy ? POLL_BUSY_MS : POLL_IDLE_MS);
}

async function showPlan() {
  $('m-plan').disabled = true;
  $('m-plan-summary').textContent = '正在检查移动计划…';
  $('m-plan-view').hidden = false;
  try {
    const plan = await api(`/api/organize/moves/plan?${new URLSearchParams({ root: view.root })}`);
    move.plan = plan;
    const held = plan.problems.length ? `；${plan.problems.length} 个已确认的包先不动，原因见下` : '';
    $('m-plan-summary').textContent = plan.moves
      ? `可以移动 ${plan.moves} 个已确认的包（共 ${plan.files} 个文件）${held}。`
      : `没有可以移动的包：先在下面确认建议${held}。`;
    $('m-plan-problems').replaceChildren(...problemList(plan.problems));
    $('m-plan-problems-box').hidden = !plan.problems.length;
    $('m-plan-sample').replaceChildren(...plan.sample.map((m) => el('li', {}, [
      el('span', { className: 'org-move-path', text: m.path }), el('span', { className: 'arrow', text: ' → ' }), el('strong', { text: m.target }),
      ...(m.renames.length > 1 ? [el('span', { className: 'muted', text: `（连同 ${m.renames.length - 1} 个预览图或分卷）` })] : []),
    ])));
    $('m-plan-sample-box').hidden = !plan.sample.length;
    if (move.state) renderMoveState(move.state);
  } catch (err) {
    $('m-plan-summary').textContent = `检查失败：${err.message}`;
  } finally {
    $('m-plan').disabled = false;
  }
}

async function startMove(body, question) {
  if (question && !window.confirm(question)) return;
  $('m-error').textContent = '';
  try {
    await post('/api/organize/moves', body);
  } catch (err) {
    $('m-error').textContent = `没能开始：${err.message}`;  // its own line: the status poll does not overwrite it
  }
  refreshMoves();
}

async function startApply() {
  $('m-start').disabled = true;
  try {  // the plan as it is now: proposals may have been confirmed since it was shown
    const plan = await api(`/api/organize/moves/plan?${new URLSearchParams({ root: view.root })}`);
    if (!plan.moves) { $('m-error').textContent = '现在没有可以移动的包。'; return; }
    await startMove({ action: 'apply', root: view.root },
      `把 ${plan.moves} 个已确认的包（${plan.files} 个文件）移动到分类文件夹？每一步都有记录，之后可以整次撤销。`);
  } catch (err) {
    $('m-error').textContent = `没能开始：${err.message}`;
  } finally {
    if (move.state) renderMoveState(move.state);
  }
}

function initMoves() {
  $('m-plan').addEventListener('click', showPlan);
  $('m-start').addEventListener('click', startApply);
  $('m-recover').addEventListener('click', () => startMove({ action: 'recover', root: view.root }));
  $('m-stop').addEventListener('click', async () => {
    try { await post('/api/organize/moves/stop', {}); } catch (err) { $('m-error').textContent = `没能停止：${err.message}`; }
    refreshMoves();
  });
  $('o-form').root.addEventListener('change', () => { move.plan = null; $('m-plan-view').hidden = true; refreshMoves(); });
  refreshMoves();
}

initMoves();
