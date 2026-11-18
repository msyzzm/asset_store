'use strict';

// The "上传" dialog (from the header or the task bar row), and files or
// folders dropped on the page: sent to the web server (assetindex/upload_web.py), landing
// in the library's _下载 folder. Several files are one upload each; a folder is one
// upload, one pack. Loaded after downloads.js (dlPost, refreshDownloads), captions.js
// (setText) and app.js (formatSize).

const UP_PARALLEL = 3;  // files of a folder sent at once
const UP_TRIES = 3;  // a file whose connection broke is sent again, this many times in all
const UP_RETRY_MS = 2000;  // times the try number, before sending again
const UP_FINISH_WAIT_MS = 5000;  // a move job holds the landing back: asked again after this
// What the server takes as a path (downloads.check_out, uploads.MAX_PATH/MAX_DEPTH)
const UP_BAD_CHARS = /[\x00-\x1f\x7f\\:*?"<>|]/;
const UP_MAX_NAME_BYTES = 255;
const UP_MAX_PATH_BYTES = 1000;
const UP_MAX_DEPTH = 64;
const UP_LISTED = 3;  // unusable names shown in the note
const up = { busy: false };
const upBytes = (s) => new TextEncoder().encode(s).length;
const upWait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function upHidden(path) {
  return path.split('/').some((name) => name.startsWith('.'));
}

function upBadPath(path) {
  const names = path.split('/');
  return upBytes(path) > UP_MAX_PATH_BYTES || names.length > UP_MAX_DEPTH || path.toLowerCase().endsWith('.aria2')
    || names.some((n) => !n || n !== n.trim() || UP_BAD_CHARS.test(n) || upBytes(n) > UP_MAX_NAME_BYTES);
}

// [{file, path}] split into what is sent and notes on what is not
function upSort(entries) {
  const hidden = entries.filter((e) => upHidden(e.path));
  const bad = entries.filter((e) => !upHidden(e.path) && upBadPath(e.path));
  const kept = entries.filter((e) => !upHidden(e.path) && !upBadPath(e.path));
  const notes = [];
  if (hidden.length) notes.push(`${hidden.length} 个隐藏文件没传`);
  if (bad.length) {
    const names = bad.slice(0, UP_LISTED).map((e) => e.path).join('、');
    notes.push(`${bad.length} 个文件名不能用，没传：${names}${bad.length > UP_LISTED ? ' 等' : ''}`);
  }
  return { kept, notes };
}

function upError(message, status) {
  const err = new Error(message);
  err.status = status;
  return err;
}

function upPut(url, file, onProgress, running) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();  // fetch() cannot tell how much of a body went out
    running.add(xhr);
    xhr.onloadend = () => running.delete(xhr);
    xhr.onabort = () => reject(upError('已中止', -1));
    xhr.open('PUT', url);
    xhr.setRequestHeader('X-Requested-With', 'fetch');
    xhr.upload.onprogress = (event) => onProgress(event.loaded);
    xhr.onload = () => {
      if (xhr.status < 300) return resolve();
      let detail = '';
      try { detail = JSON.parse(xhr.responseText).detail; } catch { /* not JSON */ }
      reject(upError(typeof detail === 'string' && detail ? detail : `HTTP ${xhr.status}`, xhr.status));
    };
    xhr.onerror = () => reject(upError('连接断了', 0));
    xhr.send(file);
  });
}

// A broken connection or a server error is worth another try; a refusal is not.
async function upPutRetrying(url, file, onProgress, running, isStopped) {
  for (let attempt = 1; ; attempt++) {
    try {
      return await upPut(url, file, onProgress, running);
    } catch (err) {
      const transient = err.status === 0 || err.status >= 500;
      if (!transient || attempt >= UP_TRIES || isStopped()) throw err;
      onProgress(0);
      await upWait(UP_RETRY_MS * attempt);
      if (isStopped()) throw err;
    }
  }
}

async function upFinish(job, id) {
  for (;;) {
    try {
      return await dlPost(`/api/uploads/${id}/finish`);
    } catch (err) {
      if (!err.message.includes('整理正在移动')) throw err;
      setText($('u-status'), `${job.label} 已传完，等整理移动文件结束后放进 _下载…`);
      await upWait(UP_FINISH_WAIT_MS);
    }
  }
}

async function upSend(job, progress) {
  const row = await dlPost('/api/uploads', job.body);
  refreshDownloads();
  const sent = job.files.map(() => 0);
  const running = new Set();  // requests on their way, stopped together when one file fails
  let next = 0;
  let stopped = false;
  const report = (i, n) => {
    if (stopped) return;
    sent[i] = n;
    progress(sent);
  };
  const worker = async () => {
    while (!stopped && next < job.files.length) {
      const i = next++;
      await upPutRetrying(`/api/uploads/${row.id}/files/${i}`, job.files[i], (n) => report(i, n), running,
        () => stopped);
      report(i, job.files[i].size);
    }
  };
  const workers = Array.from({ length: Math.min(UP_PARALLEL, job.files.length) }, worker);
  try {
    await Promise.all(workers);
    return await upFinish(job, row.id);
  } catch (err) {
    stopped = true;
    running.forEach((xhr) => xhr.abort());
    await Promise.allSettled(workers);  // none still sending when the next upload starts
    dlPost(`/api/downloads/${row.id}/cancel`).catch(() => {});  // nothing half-sent left on the NAS
    throw err;
  }
}

async function upRun(jobs, notes = []) {
  const note = notes.length ? `（${notes.join('；')}）` : '';
  if (up.busy) return setText($('u-status'), '正在上传另一批，传完再加。');
  if (!jobs.length) return setText($('u-status'), `没有可传的文件${note}`);
  up.busy = true;
  $('u-files').disabled = $('u-folder').disabled = true;
  const bar = $('u-bar');
  bar.hidden = false;
  const failed = [];
  for (const [n, job] of jobs.entries()) {
    const total = job.files.reduce((sum, f) => sum + f.size, 0);
    const count = jobs.length > 1 ? `（${n + 1}/${jobs.length}）` : '';
    const progress = (sent) => {
      const done = sent.reduce((sum, s) => sum + s, 0);
      bar.max = Math.max(1, total);
      bar.value = done;
      setText($('u-status'), `正在上传${count} ${job.label}：${formatSize(done)}/${formatSize(total)}`);
    };
    progress(job.files.map(() => 0));
    try {
      await upSend(job, progress);
    } catch (err) {
      failed.push(`${job.label}：${err.message}`);
    }
    refreshDownloads();
  }
  up.busy = false;
  $('u-files').disabled = $('u-folder').disabled = false;
  bar.hidden = true;
  const ok = jobs.length - failed.length;
  return setText($('u-status'), (failed.length ? `传完 ${ok} 个，失败 ${failed.length} 个：${failed.join('；')}` : `传完 ${ok} 个，扫描后就能搜到。`) + note);
}

// Loose files: one upload each
function upFileJobs(files) {
  const { kept, notes } = upSort(files.map((f) => ({ file: f, path: f.name })));
  return {
    notes,
    jobs: kept.map(({ file }) => ({ label: file.name, files: [file], body: { files: [{ path: file.name, size: file.size }] } })),
  };
}

// A folder: [{file, path}] with paths inside it, one upload
function upFolderJob(name, entries) {
  const { kept, notes } = upSort(entries);
  return {
    notes,
    job: kept.length ? {
      label: name, files: kept.map((e) => e.file),
      body: { folder: true, name, files: kept.map((e) => ({ path: e.path, size: e.file.size })) },
    } : null,
  };
}

function upOpenDialog() {
  if ($('uploads').open) return;
  if ($('downloads').open) $('downloads').close();
  $('uploads').showModal();
  refreshDownloads();
}

for (const id of ['upload-open', 'uploads-task-open']) $(id).addEventListener('click', upOpenDialog);
$('u-close').addEventListener('click', () => $('uploads').close());
$('u-files').addEventListener('click', () => $('u-files-input').click());
$('u-folder').addEventListener('click', () => $('u-folder-input').click());

$('u-files-input').addEventListener('change', (event) => {
  const files = [...event.target.files];
  event.target.value = '';
  const { jobs, notes } = upFileJobs(files);
  upRun(jobs, notes);
});

$('u-folder-input').addEventListener('change', (event) => {
  const files = [...event.target.files];
  event.target.value = '';
  if (!files.length) return setText($('u-status'), '这个文件夹是空的。');
  // webkitRelativePath is "<chosen folder>/sub/a.png": the folder's own name is the pack's
  const name = files[0].webkitRelativePath.split('/')[0];
  const entries = files.map((f) => ({ file: f, path: f.webkitRelativePath.split('/').slice(1).join('/') }));
  const { job, notes } = upFolderJob(name, entries);
  return upRun(job ? [job] : [], notes);
});

// Dropped on the page: files and folders, read through the entries API

function upEntryFile(entry) {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

async function upReadFolder(folder, prefix = '', out = []) {
  const reader = folder.createReader();
  for (;;) {
    const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));  // part of it each call
    if (!batch.length) return out;
    for (const entry of batch) {
      const path = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory) await upReadFolder(entry, path, out);
      else out.push({ file: await upEntryFile(entry), path });
    }
  }
}

async function upDropped(entries) {
  upOpenDialog();
  if (up.busy) return setText($('u-status'), '正在上传另一批，传完再加。');
  setText($('u-status'), '正在读取拖进来的文件…');
  try {
    const files = await Promise.all(entries.filter((e) => e.isFile).map(upEntryFile));
    const { jobs, notes } = upFileJobs(files);
    for (const folder of entries.filter((e) => e.isDirectory)) {
      const found = await upReadFolder(folder);
      const made = upFolderJob(folder.name, found);
      if (made.job) jobs.push(made.job);
      else if (!found.length) notes.push(`${folder.name} 是空的`);
      notes.push(...made.notes.map((n) => `${folder.name}：${n}`));
    }
    return await upRun(jobs, notes);
  } catch (err) {
    return setText($('u-status'), `读取拖进来的文件失败：${err.message}`);
  }
}

const upDrag = { depth: 0 };  // dragenter/dragleave fire for every element crossed
const upHasFiles = (event) => event.dataTransfer?.types.includes('Files');

document.addEventListener('dragenter', (event) => {
  if (!upHasFiles(event)) return;
  upDrag.depth += 1;
  document.body.classList.add('drop-target');
});
document.addEventListener('dragleave', (event) => {
  if (!upHasFiles(event)) return;
  upDrag.depth = Math.max(0, upDrag.depth - 1);
  if (!upDrag.depth) document.body.classList.remove('drop-target');
});
document.addEventListener('dragover', (event) => {
  if (!upHasFiles(event)) return;
  event.preventDefault();  // or the browser opens the file instead
  event.dataTransfer.dropEffect = 'copy';
});
document.addEventListener('drop', (event) => {
  if (!upHasFiles(event)) return;
  event.preventDefault();
  upDrag.depth = 0;
  document.body.classList.remove('drop-target');
  // The entries can only be taken during the event; they are read after it
  const entries = [...event.dataTransfer.items].map((i) => i.webkitGetAsEntry?.()).filter(Boolean);
  if (entries.length) upDropped(entries);
});

window.addEventListener('beforeunload', (event) => {
  if (up.busy) event.preventDefault();  // leaving drops the upload: there is no resuming
});
