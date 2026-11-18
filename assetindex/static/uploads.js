'use strict';

// "上传文件…" and "上传文件夹…" in the downloads dialog: files from this computer sent to
// the web server (assetindex/upload_web.py), landing in the library's _下载 folder. Several
// files are one upload each; a folder is one upload, one pack. Loaded after downloads.js
// (dlPost, refreshDownloads), captions.js (setText) and app.js (formatSize).

const UP_PARALLEL = 3;  // files of a folder sent at once
const UP_FINISH_WAIT_MS = 5000;  // a move job holds the landing back: asked again after this
const up = { busy: false };

function upHidden(path) {
  return path.split('/').some((name) => name.startsWith('.'));
}

function upPut(url, file, onProgress, running) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();  // fetch() cannot tell how much of a body went out
    running.add(xhr);
    xhr.onloadend = () => running.delete(xhr);
    xhr.onabort = () => reject(new Error('已中止'));
    xhr.open('PUT', url);
    xhr.setRequestHeader('X-Requested-With', 'fetch');
    xhr.upload.onprogress = (event) => onProgress(event.loaded);
    xhr.onload = () => {
      if (xhr.status < 300) return resolve();
      let detail = '';
      try { detail = JSON.parse(xhr.responseText).detail; } catch { /* not JSON */ }
      reject(new Error(typeof detail === 'string' && detail ? detail : `HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error('连接断了'));
    xhr.send(file);
  });
}

async function upFinish(job, id) {
  for (;;) {
    try {
      return await dlPost(`/api/uploads/${id}/finish`);
    } catch (err) {
      if (!err.message.includes('整理正在移动')) throw err;
      setText($('u-status'), `${job.label} 已传完，等整理移动文件结束后放进 _下载…`);
      await new Promise((resolve) => setTimeout(resolve, UP_FINISH_WAIT_MS));
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
      await upPut(`/api/uploads/${row.id}/files/${i}`, job.files[i], (n) => report(i, n), running);
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

async function upRun(jobs, note = '') {
  if (up.busy || !jobs.length) return;
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
  setText($('u-status'), (failed.length ? `传完 ${ok} 个，失败 ${failed.length} 个：${failed.join('；')}` : `传完 ${ok} 个，扫描后就能搜到。`) + note);
}

function upFileJobs(files) {
  return files.filter((f) => !upHidden(f.name)).map((f) => ({
    label: f.name, files: [f], body: { files: [{ path: f.name, size: f.size }] },
  }));
}

function upFolderJob(files) {
  // webkitRelativePath is "<chosen folder>/sub/a.png": the folder's own name is the pack's
  const entries = files.map((f) => ({ file: f, path: f.webkitRelativePath.split('/').slice(1).join('/') }));
  const kept = entries.filter((e) => !upHidden(e.path));
  const name = files[0].webkitRelativePath.split('/')[0];
  const skipped = entries.length - kept.length;
  return {
    note: skipped ? `（${skipped} 个隐藏文件没传）` : '',
    job: kept.length ? {
      label: name, files: kept.map((e) => e.file),
      body: { folder: true, name, files: kept.map((e) => ({ path: e.path, size: e.file.size })) },
    } : null,
  };
}

$('u-files').addEventListener('click', () => $('u-files-input').click());
$('u-folder').addEventListener('click', () => $('u-folder-input').click());

$('u-files-input').addEventListener('change', (event) => {
  const files = [...event.target.files];
  event.target.value = '';
  upRun(upFileJobs(files));
});

$('u-folder-input').addEventListener('change', (event) => {
  const files = [...event.target.files];
  event.target.value = '';
  if (!files.length) return setText($('u-status'), '这个文件夹是空的。');
  const { job, note } = upFolderJob(files);
  if (!job) return setText($('u-status'), `文件夹里只有隐藏文件，没有可传的${note}`);
  return upRun([job], note);
});

window.addEventListener('beforeunload', (event) => {
  if (up.busy) event.preventDefault();  // leaving drops the upload: there is no resuming
});
