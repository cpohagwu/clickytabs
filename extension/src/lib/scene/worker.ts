/**
 * The scene runtime: a dedicated Worker, created from an inline blob by the sandbox page (scene-harness), that
 * runs model-written three.js or 2D-canvas code on an OffscreenCanvas.
 *
 * Why a Worker: the code has no DOM, no `window`, no `location` - it cannot navigate the sandbox frame or draw
 * anything outside its canvas. It inherits the sandbox CSP, so fetch/XHR/WebSocket are blocked (connect-src 'none').
 * A runaway loop only freezes this Worker: the page notices the missing heartbeats and terminates it.
 *
 * The code runs inside an async function with the scene API in scope (`THREE, scene, camera, ...`, and `ctx` holding
 * the same names), plus small `window`/`document` stand-ins for web-page habits (compat.ts). Errors are reported with
 * the line inside the scene code. Success means: the code finished, FRAMES_TO_SETTLE frames rendered without an
 * error, and something visible was drawn.
 */
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';

import { type AsyncFunctionConstructor, compile, describeError, makeShims } from './compat';
import type { ForwardedEvent, RenderStats, WorkerReply, WorkerRequest } from './types';

const FRAMES_TO_SETTLE = 20;
const SETUP_TIMEOUT_MS = 10_000;
const HEARTBEAT_MS = 250;
const MAX_LABEL_CHARS = 80;

type FrameCallback = (dt: number, t: number) => void;

// Captured before any scene code runs: the code can replace self.postMessage, not these.
const post = self.postMessage.bind(self) as (message: WorkerReply) => void;
const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor as AsyncFunctionConstructor;

let token = '';
let started = false;
let failed = false;
let ready = false;
const frameCallbacks: FrameCallback[] = [];
let onResize: ((width: number, height: number, dpr: number) => void) | null = null;
let onPointer: ((event: ForwardedEvent) => void) | null = null;

type Unsigned<T> = T extends unknown ? Omit<T, 'token'> : never;

function send(message: Unsigned<WorkerReply>): void {
  post({ ...message, token } as WorkerReply);
}

function fail(phase: 'compile' | 'setup' | 'frame' | 'blank', error: unknown, code: string): void {
  if (failed) return;
  failed = true;
  send({ type: 'error', phase, message: typeof error === 'string' ? error : describeError(error, code) });
}

// ------------------------------------------------------------------------------------------ OrbitControls proxy

/**
 * Stands in for the canvas element OrbitControls listens to: it receives the pointer, wheel and key events the
 * sandbox page forwards, and acts as its own ownerDocument (OrbitControls adds pointermove/up listeners there).
 */
class ElementProxy extends THREE.EventDispatcher<Record<string, Record<string, unknown>>> {
  clientWidth = 1;
  clientHeight = 1;
  readonly style: { cursor: string; touchAction: string };
  readonly ownerDocument = this;

  constructor() {
    super();
    let cursor = 'auto';
    this.style = {
      get cursor() {
        return cursor;
      },
      set cursor(value: string) {
        cursor = value;
        send({ type: 'cursor', value });
      },
      touchAction: 'none',
    };
  }

  getRootNode(): this {
    return this;
  }

  getBoundingClientRect(): DOMRect {
    const { clientWidth: width, clientHeight: height } = this;
    return { left: 0, top: 0, x: 0, y: 0, width, height, right: width, bottom: height, toJSON: () => ({}) } as DOMRect;
  }

  setPointerCapture(): void {}
  releasePointerCapture(): void {}
  hasPointerCapture(): boolean {
    return false;
  }
  focus(): void {}

  forward(event: ForwardedEvent): void {
    const synthetic = {
      ...event,
      pageX: event.clientX,
      pageY: event.clientY,
      preventDefault() {},
      stopPropagation() {},
    };
    this.dispatchEvent(synthetic as never);
  }
}

// ------------------------------------------------------------------------------------------------------ labels

function labelSprite(text: string, position: unknown, options: { color?: string; size?: number; background?: boolean } = {}): THREE.Sprite {
  const label = String(text).slice(0, MAX_LABEL_CHARS);
  const fontPx = 48;
  const padX = 22;
  const measure = new OffscreenCanvas(8, 8).getContext('2d')!;
  measure.font = `600 ${fontPx}px system-ui, "Segoe UI", sans-serif`;
  const width = Math.ceil(measure.measureText(label).width) + padX * 2;
  const height = Math.round(fontPx * 1.6);
  const canvas = new OffscreenCanvas(width, height);
  const g = canvas.getContext('2d')!;
  if (options.background !== false) {
    g.fillStyle = 'rgba(10, 14, 30, 0.78)';
    g.strokeStyle = 'rgba(124, 240, 255, 0.55)';
    g.lineWidth = 3;
    g.beginPath();
    g.roundRect(1.5, 1.5, width - 3, height - 3, height / 2.4);
    g.fill();
    g.stroke();
  }
  g.font = measure.font;
  g.fillStyle = options.color ?? '#ffffff';
  g.textBaseline = 'middle';
  g.fillText(label, padX, height / 2 + 2);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  const material = new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true });
  const sprite = new THREE.Sprite(material);
  const scale = 0.42 * (options.size ?? 1);
  sprite.scale.set((width / height) * scale, scale, 1);
  sprite.renderOrder = 999;
  sprite.userData.ccLabel = true;
  if (position instanceof THREE.Vector3) sprite.position.copy(position);
  else if (Array.isArray(position)) sprite.position.set(Number(position[0]) || 0, Number(position[1]) || 0, Number(position[2]) || 0);
  else if (position && typeof position === 'object') {
    const p = position as { x?: number; y?: number; z?: number };
    sprite.position.set(p.x ?? 0, p.y ?? 0, p.z ?? 0);
  }
  return sprite;
}

// --------------------------------------------------------------------------------------------------- runtimes

function onFrame(callback: unknown): void {
  if (typeof callback === 'function') frameCallbacks.push(callback as FrameCallback);
}

/** The code that actually runs (a cleaned-up variant when the original didn't compile): error lines refer to it. */
let activeCode = '';

async function runCode(code: string, api: Record<string, unknown>): Promise<boolean> {
  activeCode = code;
  const compiled = compile(AsyncFunction, Object.keys(api), code);
  if (!compiled.ok) {
    fail('compile', compiled.error, code);
    return false;
  }
  activeCode = compiled.code;
  try {
    const setup = compiled.run(api);
    const timeout = new Promise<never>((_, reject) =>
      setTimeout(() => reject(new Error('the scene setup did not finish within 10 s')), SETUP_TIMEOUT_MS));
    await Promise.race([setup, timeout]);
    return true;
  } catch (error) {
    fail('setup', error, activeCode);
    return false;
  }
}

/**
 * THREE as the scene code sees it: Scene and cameras the code makes itself are tracked (so they can be adopted), a
 * WebGLRenderer it makes is the runtime's own, and addons that don't exist here fail with a useful message.
 */
function trackedThree(renderer: THREE.WebGLRenderer, controlsFor: (camera: THREE.Camera) => OrbitControls): {
  ns: Record<string, unknown>; scenes: THREE.Scene[]; cameras: THREE.Camera[];
} {
  const scenes: THREE.Scene[] = [];
  const cameras: THREE.Camera[] = [];
  class Scene extends THREE.Scene {
    constructor() {
      super();
      scenes.push(this);
    }
  }
  class PerspectiveCamera extends THREE.PerspectiveCamera {
    constructor(...args: ConstructorParameters<typeof THREE.PerspectiveCamera>) {
      super(...args);
      cameras.push(this);
    }
  }
  class OrthographicCamera extends THREE.OrthographicCamera {
    constructor(...args: ConstructorParameters<typeof THREE.OrthographicCamera>) {
      super(...args);
      cameras.push(this);
    }
  }
  const unavailable = (name: string, hint: string) => function missing(): never {
    throw new Error(`THREE.${name} is not available in scenes: ${hint}`);
  };
  class TextureLoader {
    load(): THREE.Texture {
      return new THREE.Texture();                     // files can't load here (no network): an empty texture
    }
    async loadAsync(): Promise<THREE.Texture> {
      return new THREE.Texture();
    }
  }
  const ns: Record<string, unknown> = {
    ...THREE, Scene, PerspectiveCamera, OrthographicCamera, TextureLoader,
    WebGLRenderer: function WebGLRenderer() {
      return renderer;
    },
    OrbitControls: function Controls(camera: THREE.Camera) {
      return controlsFor(camera);
    },
    TextGeometry: unavailable('TextGeometry', 'use addLabel(text, position) for text'),
    FontLoader: unavailable('FontLoader', 'use addLabel(text, position) for text'),
    GLTFLoader: unavailable('GLTFLoader', 'build the model from THREE geometries'),
  };
  return { ns, scenes, cameras };
}

function hasLight(root: THREE.Object3D): boolean {
  let found = false;
  root.traverse((object) => {
    if ((object as THREE.Light).isLight) found = true;
  });
  return found;
}

function countDrawables(root: THREE.Object3D): number {
  let count = 0;
  root.traverseVisible((object) => {
    const drawable = object as THREE.Mesh;
    if ((drawable.isMesh || (object as THREE.Line).isLine || (object as THREE.Points).isPoints) && !object.userData.ccLabel) {
      count += 1;
    }
  });
  return count;
}

/** Share of sampled pixels with any opacity (the stage behind is transparent until something is drawn). */
function paintedRatio(pixels: Uint8Array | Uint8ClampedArray, stride = 4 * 7): number {
  let painted = 0;
  let total = 0;
  for (let i = 3; i < pixels.length; i += stride) {
    total += 1;
    if (pixels[i]! > 8) painted += 1;
  }
  return total ? painted / total : 0;
}

async function startThree(request: Extract<WorkerRequest, { type: 'init' }>): Promise<void> {
  const { canvas, code } = request;
  let { width, height } = request;
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(request.dpr, 2));
  renderer.setSize(width, height, false);
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(50, width / height, 0.1, 1000);
  camera.position.set(6, 4, 8);
  camera.lookAt(0, 0, 0);
  scene.add(new THREE.HemisphereLight(0xdde6ff, 0x1a1f33, 1.6));
  const key = new THREE.DirectionalLight(0xffffff, 2.4);
  key.position.set(5, 8, 6);
  scene.add(key);

  const element = new ElementProxy();
  element.clientWidth = width;
  element.clientHeight = height;
  const controls = new OrbitControls(camera, element as unknown as HTMLElement);
  controls.enableDamping = true;
  controls.target.set(0, 0, 0);
  // What gets rendered: ours, unless the code built its own scene / camera (adopted after setup).
  let activeScene: THREE.Scene = scene;
  let activeCamera: THREE.Camera = camera;
  const fitCamera = (target: THREE.Camera): void => {
    if ((target as THREE.PerspectiveCamera).isPerspectiveCamera) {
      (target as THREE.PerspectiveCamera).aspect = width / height;
      (target as THREE.PerspectiveCamera).updateProjectionMatrix();
    }
  };
  // `canvas.style` is touched by renderer.setSize(w, h) with style updates (web-page habit): give it one.
  Object.assign(canvas, { style: {} });
  const shims = makeShims({
    size: () => ({ width, height }), dpr: request.dpr, primaryCanvas: null,
    newCanvas: (w, h) => Object.assign(new OffscreenCanvas(w, h), { style: {} }),
  });
  const tracked = trackedThree(renderer, (target) => {
    controls.object = target as THREE.PerspectiveCamera;
    return controls;
  });
  onPointer = (event) => element.forward(event);
  onResize = (w, h, dpr) => {
    width = w;
    height = h;
    element.clientWidth = w;
    element.clientHeight = h;
    renderer.setPixelRatio(Math.min(dpr, 2));
    renderer.setSize(w, h, false);
    fitCamera(camera);
    if (activeCamera !== camera) fitCamera(activeCamera);
    api.width = w;
    api.height = h;
    shims.fire('resize');
  };

  const api: Record<string, unknown> = {
    THREE: tracked.ns, scene, camera, renderer, controls, onFrame, width, height,
    OrbitControls: tracked.ns.OrbitControls,
    window: shims.window, document: shims.document,
    requestAnimationFrame: (callback: FrameRequestCallback) => requestAnimationFrame(callback),
    // Labels go to the scene the code is building (its own, if it made one); groups can re-parent them.
    addLabel: (text: string, position: unknown, options?: { color?: string; size?: number; background?: boolean }) => {
      const sprite = labelSprite(text, position, options);
      (tracked.scenes.at(-1) ?? scene).add(sprite);
      return sprite;
    },
  };
  api.ctx = api;
  if (!(await runCode(code, api))) return;
  shims.fire('DOMContentLoaded');
  shims.fire('load');
  renderer.setSize(width, height, false);                  // the code may have resized "its" renderer
  if (!countDrawables(scene)) {
    const own = [...tracked.scenes].reverse().find((candidate) => countDrawables(candidate) > 0);
    if (own) {
      activeScene = own;
      if (!hasLight(own)) own.add(new THREE.HemisphereLight(0xdde6ff, 0x1a1f33, 1.6), key.clone());
    }
  }
  const ownCamera = tracked.cameras.at(-1);
  if (ownCamera) {
    activeCamera = ownCamera;
    controls.object = ownCamera as THREE.PerspectiveCamera;
    fitCamera(ownCamera);
  }

  const clock = new THREE.Clock();
  let frames = 0;
  let lastBeat = 0;
  renderer.setAnimationLoop(() => {
    if (failed) {
      renderer.setAnimationLoop(null);
      return;
    }
    const dt = Math.min(clock.getDelta(), 0.1);
    const t = clock.elapsedTime;
    try {
      for (const callback of frameCallbacks) callback(dt, t);
      controls.update(dt);
      renderer.render(activeScene, activeCamera);
    } catch (error) {
      if (ready) send({ type: 'error', phase: 'frame', message: describeError(error, activeCode) });
      else fail('frame', error, activeCode);
      failed = true;
      renderer.setAnimationLoop(null);
      return;
    }
    frames += 1;
    const now = performance.now();
    if (now - lastBeat > HEARTBEAT_MS) {
      lastBeat = now;
      send({ type: 'heartbeat' });
    }
    if (frames === FRAMES_TO_SETTLE && !ready) {
      const gl = renderer.getContext();
      const pixels = new Uint8Array(gl.drawingBufferWidth * gl.drawingBufferHeight * 4);
      gl.readPixels(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      const stats: RenderStats = { objects: countDrawables(activeScene), paintedRatio: round(paintedRatio(pixels)) };
      if (!stats.objects) {
        fail('blank', 'blank scene: nothing to draw was added to `scene` (no meshes, lines or points); add objects '
          + 'with scene.add(...) during setup', code);
      } else if (!stats.paintedRatio) {
        fail('blank', 'blank scene: the objects are all outside the camera view (keep them within ~5 units of the '
          + 'origin, or move the camera)', code);
      }
      else {
        ready = true;
        send({ type: 'ready', stats });
      }
    }
  });
}

async function startCanvas(request: Extract<WorkerRequest, { type: 'init' }>): Promise<void> {
  const { canvas, code } = request;
  const g = canvas.getContext('2d');
  if (!g) {
    fail('setup', 'this browser has no 2D context for OffscreenCanvas', code);
    return;
  }
  // The code draws in a fixed logical space (the stage size when it started): a later resize scales and centres it,
  // so `width`/`height` captured by the code never go stale.
  const logical = { width: request.width, height: request.height };
  const view = { scale: 1, x: 0, y: 0 };
  const pointer = { x: -1, y: -1, down: false, inside: false };
  Object.assign(canvas, { style: {}, addEventListener: () => {}, removeEventListener: () => {} });
  const shims = makeShims({
    size: () => logical, dpr: request.dpr, primaryCanvas: canvas,      // createElement('canvas') -> this canvas
    newCanvas: (w, h) => Object.assign(new OffscreenCanvas(w, h), { style: {} }),
  });
  const api: Record<string, unknown> = {
    canvas, g, onFrame, pointer, width: logical.width, height: logical.height,
    window: shims.window, document: shims.document,
    requestAnimationFrame: (callback: FrameRequestCallback) => requestAnimationFrame(callback),
  };
  // `ctx` holds the API and, for everything else, is the 2D context: `ctx.fillRect(...)` works as models expect.
  const context = g as unknown as Record<string | symbol, unknown>;
  api.ctx = new Proxy(api, {
    get: (target, prop) => {
      if (prop in target) return target[prop as string];
      const value = context[prop];
      return typeof value === 'function' ? (value as (...args: unknown[]) => unknown).bind(g) : value;
    },
    set: (target, prop, value) => {
      if (prop in target) target[prop as string] = value;
      else context[prop] = value;
      return true;
    },
    has: (target, prop) => prop in target || prop in g,
  });
  const size = (w: number, h: number, dpr: number): void => {
    const pixels = Math.min(dpr, 2);
    canvas.width = Math.round(w * pixels);
    canvas.height = Math.round(h * pixels);
    view.scale = Math.min(w / logical.width, h / logical.height);
    view.x = (w - logical.width * view.scale) / 2;
    view.y = (h - logical.height * view.scale) / 2;
    g.setTransform(pixels * view.scale, 0, 0, pixels * view.scale, pixels * view.x, pixels * view.y);
  };
  size(request.width, request.height, request.dpr);
  onResize = (w, h, dpr) => {
    size(w, h, dpr);
    shims.fire('resize');
  };
  onPointer = (event) => {
    if (event.clientX !== undefined) pointer.x = (event.clientX - view.x) / view.scale;
    if (event.clientY !== undefined) pointer.y = (event.clientY - view.y) / view.scale;
    if (event.type === 'pointerdown') pointer.down = true;
    if (event.type === 'pointerup' || event.type === 'pointercancel') pointer.down = false;
    pointer.inside = event.type !== 'pointerleave';
  };
  if (!(await runCode(code, api))) return;
  shims.fire('DOMContentLoaded');
  shims.fire('load');

  let frames = 0;
  let last = performance.now();
  const startedAt = last;
  let lastBeat = 0;
  const frame = (): void => {
    if (failed) return;
    const now = performance.now();
    const dt = Math.min((now - last) / 1000, 0.1);
    last = now;
    try {
      for (const callback of frameCallbacks) callback(dt, (now - startedAt) / 1000);
    } catch (error) {
      if (ready) send({ type: 'error', phase: 'frame', message: describeError(error, activeCode) });
      else fail('frame', error, activeCode);
      failed = true;
      return;
    }
    frames += 1;
    if (now - lastBeat > HEARTBEAT_MS) {
      lastBeat = now;
      send({ type: 'heartbeat' });
    }
    if (frames === FRAMES_TO_SETTLE && !ready) {
      const ratio = round(paintedRatio(g.getImageData(0, 0, canvas.width, canvas.height).data));
      if (!ratio) fail('blank', 'blank scene: nothing was drawn on the canvas', code);
      else {
        ready = true;
        send({ type: 'ready', stats: { paintedRatio: ratio } });
      }
    }
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

function round(value: number): number {
  return Math.round(value * 1000) / 1000;
}

self.addEventListener('unhandledrejection', (event: PromiseRejectionEvent) => {
  if (ready) send({ type: 'error', phase: 'frame', message: describeError(event.reason, '') });
});

self.addEventListener('message', (event: MessageEvent<WorkerRequest>) => {
  const message = event.data;
  if (!message || typeof message !== 'object') return;
  if (message.type === 'init' && !started) {
    started = true;
    token = message.token;
    send({ type: 'heartbeat' });
    const run = message.kind === 'three' ? startThree(message) : startCanvas(message);
    void run.catch((error: unknown) => fail('setup', error, message.code));
  } else if (message.type === 'resize') {
    onResize?.(message.width, message.height, message.dpr);
  } else if (message.type === 'event') {
    onPointer?.(message.event);
  }
});
