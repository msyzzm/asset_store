// 3D preview of an FBX file. Loaded with import() only when a model is opened.
import * as THREE from 'three';
import { FBXLoader } from 'three/addons/loaders/FBXLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const FIT_MARGIN = 1.4;
const BOUNDS_SAMPLES = 12; // clip poses sampled so a character that walks off stays in frame
// FBX files name external textures by paths on the artist's machine; never request those.
const BLANK_IMAGE = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==';

// One-frame clips are just the bind pose (common in Mixamo character files): not an animation.
const MIN_CLIP_SECONDS = 0.1;

function playableClips(clips) {
  return clips.filter((clip) => clip.duration >= MIN_CLIP_SECONDS);
}

function longestClip(clips) {
  return clips.reduce((best, clip) => (!best || clip.duration > best.duration ? clip : best), null);
}

function boneBounds(root, box) {
  const point = new THREE.Vector3();
  root.updateMatrixWorld(true);
  root.traverse((node) => {
    if (node.isBone) box.expandByPoint(node.getWorldPosition(point));
  });
}

// Mesh bounds in bind pose plus bone positions across the clip.
function sceneBounds(root, mixer, clip) {
  const box = new THREE.Box3().setFromObject(root);
  if (!clip) {
    boneBounds(root, box);
    return box;
  }
  for (let i = 0; i <= BOUNDS_SAMPLES; i += 1) {
    mixer.setTime((clip.duration * i) / BOUNDS_SAMPLES);
    boneBounds(root, box);
  }
  mixer.setTime(0);
  return box;
}

function frame(camera, controls, grid, box) {
  const size = box.isEmpty() ? new THREE.Vector3(1, 1, 1) : box.getSize(new THREE.Vector3());
  const center = box.isEmpty() ? new THREE.Vector3() : box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.length() / 2, 1e-3);
  // Fit the narrower of the vertical and horizontal fields of view (portrait phone screens).
  const halfV = THREE.MathUtils.degToRad(camera.fov / 2);
  const halfFov = Math.min(halfV, Math.atan(Math.tan(halfV) * camera.aspect));
  const distance = (radius * FIT_MARGIN) / Math.sin(halfFov);
  camera.near = distance / 100;
  camera.far = distance * 100;
  camera.position.copy(center).add(new THREE.Vector3(0.6, 0.4, 1).normalize().multiplyScalar(distance));
  camera.updateProjectionMatrix();
  controls.target.copy(center);
  controls.update();
  grid.scale.setScalar(radius * 4);
  grid.position.y = box.isEmpty() ? 0 : box.min.y;
}

function disposeTree(root) {
  root.traverse((node) => {
    node.geometry?.dispose();
    for (const material of [].concat(node.material ?? [])) {
      for (const value of Object.values(material)) if (value?.isTexture) value.dispose();
      material.dispose();
    }
  });
}

function describe(root, clip) {
  let meshes = 0;
  let bones = 0;
  root.traverse((node) => {
    if (node.isMesh) meshes += 1;
    if (node.isBone) bones += 1;
  });
  const parts = [meshes ? `${meshes} 个网格` : '无网格（显示骨骼）', `${bones} 根骨骼`];
  if (clip) parts.push(`动画 ${clip.duration.toFixed(1)} 秒`);
  return parts.join(' · ');
}

function buildScene(container) {
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  container.append(renderer.domElement);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x1d2026);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x444455, 2.2));
  const sun = new THREE.DirectionalLight(0xffffff, 2);
  sun.position.set(1, 2, 1.5);
  scene.add(sun);
  const grid = new THREE.GridHelper(1, 20, 0x556070, 0x343a44);
  scene.add(grid);
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 1000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  return { renderer, scene, grid, camera, controls };
}

function animationControls(mixer, clips, onPick) {
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'btn secondary';
  toggle.textContent = '暂停';
  toggle.addEventListener('click', () => {
    mixer.timeScale = mixer.timeScale ? 0 : 1;
    toggle.textContent = mixer.timeScale ? '暂停' : '播放';
  });
  if (clips.length < 2) return [toggle];
  const select = document.createElement('select');
  select.setAttribute('aria-label', '动画');
  clips.forEach((clip, i) => {
    select.append(new Option(`${clip.name || `动画 ${i + 1}`}（${clip.duration.toFixed(1)}s）`, String(i)));
  });
  select.value = String(clips.indexOf(longestClip(clips)));
  select.addEventListener('change', () => onPick(clips[Number(select.value)]));
  return [toggle, select];
}

function hasMesh(root) {
  let found = false;
  root.traverse((node) => { if (node.isMesh) found = true; });
  return found;
}

/**
 * Render the FBX at `url` into `container`. Returns a function that stops rendering,
 * aborts the download and frees GPU memory (browsers cap live WebGL contexts).
 * @param {HTMLElement} container
 * @param {string} url
 * @param {(text: string, isError?: boolean) => void} onStatus
 * @param {HTMLElement} toolbar receives play/pause and clip controls
 * @param {number} maxBytes refuse responses larger than this
 * @returns {() => void}
 */
export function mountModelViewer(container, url, onStatus, toolbar, maxBytes) {
  const abort = new AbortController();
  const { renderer, scene, grid, camera, controls } = buildScene(container);
  const clock = new THREE.Clock();
  let mixer = null;
  let model = null;
  let helper = null;
  let frameId = 0;
  let disposed = false;
  const blobs = new Set();

  const resize = () => {
    const { clientWidth: w, clientHeight: h } = container;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  };
  const observer = new ResizeObserver(resize);
  observer.observe(container);
  resize();

  const tick = () => {
    frameId = requestAnimationFrame(tick);
    mixer?.update(clock.getDelta());
    controls.update();
    renderer.render(scene, camera);
  };
  tick();

  async function load() {
    onStatus('下载中…');
    const resp = await fetch(url, { signal: abort.signal });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    // The index's size may be stale (file replaced on the NAS since the scan): check the real one.
    if (Number(resp.headers.get('Content-Length')) > maxBytes) {
      abort.abort();
      throw new Error('文件已变大，超过预览上限，请下载后查看');
    }
    const buffer = await resp.arrayBuffer();
    if (disposed) return;
    onStatus('解析中…');
    await new Promise((resolve) => setTimeout(resolve, 0)); // let the status paint before parsing blocks
    if (disposed) return;
    const manager = new THREE.LoadingManager();
    manager.setURLModifier((u) => {
      if (u.startsWith('blob:')) {
        blobs.add(u); // embedded textures; FBXLoader never revokes them
        return u;
      }
      return u.startsWith('data:') ? u : BLANK_IMAGE;
    });
    model = new FBXLoader(manager).parse(buffer, '');
    scene.add(model);

    const clips = playableClips(model.animations);
    const clip = longestClip(clips);
    mixer = new THREE.AnimationMixer(model);
    let action = clip ? mixer.clipAction(clip) : null;
    action?.play(); // before sceneBounds: the mixer only poses bones for active actions
    if (!hasMesh(model)) {
      helper = new THREE.SkeletonHelper(model);
      scene.add(helper);
    }
    frame(camera, controls, grid, sceneBounds(model, mixer, clip));
    clock.getDelta(); // don't jump ahead by the parse time
    onStatus(describe(model, clip));
    if (!clip) return;
    toolbar.replaceChildren(...animationControls(mixer, clips, (picked) => {
      action.stop();
      action = mixer.clipAction(picked);
      action.play();
      frame(camera, controls, grid, sceneBounds(model, mixer, picked)); // clips differ in root motion
      onStatus(describe(model, picked));
    }));
  }

  load().catch((err) => {
    if (!disposed) onStatus(`无法预览此模型：${err.message}`, true);
  });

  return () => {
    disposed = true;
    abort.abort();
    cancelAnimationFrame(frameId);
    observer.disconnect();
    controls.dispose();
    mixer?.stopAllAction();
    if (model) {
      mixer.uncacheRoot(model);
      disposeTree(model);
    }
    blobs.forEach((u) => URL.revokeObjectURL(u));
    helper?.dispose();
    grid.dispose();
    renderer.dispose();
    renderer.forceContextLoss();
    renderer.domElement.remove();
  };
}
