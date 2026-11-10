/**
 * Runs inside public/scene.html, a SANDBOXED extension page (opaque origin, no extension APIs, eval allowed, no
 * fetch/XHR/WebSocket). Loaded as a classic script: module scripts are fetched in CORS mode, which an opaque origin
 * cannot satisfy for extension resources.
 *
 * Handshake: accepts exactly one `{type:"init", nonce}` message from its parent whose nonce equals this page's URL
 * hash and which transfers a MessagePort; afterwards it ignores window messages and serves only that port.
 *
 * Scenes (port messages, see lib/scene/types.ts):
 * - `render` three/canvas: a <canvas> handed to a Worker as an OffscreenCanvas; the Worker (lib/scene/worker.ts,
 *   inlined here as a blob: worker, hence `worker-src blob:` in the sandbox CSP) runs the code. This page forwards
 *   pointer/wheel/key events and resizes, and watches the Worker's heartbeat: a frozen Worker is terminated.
 * - `render` svg/html: sanitized markup (lib/scene/sanitize.ts) adopted into this page; no code runs.
 * - Each render answers one `result` (ok, or why not); a scene that breaks later sends `late-error`.
 */
import SceneWorker from '@/lib/scene/worker?worker&inline';
import { sanitizeScene } from '@/lib/scene/sanitize';
import {
  type ForwardedEvent,
  isSceneKind,
  MAX_SCENE_CODE_CHARS,
  type RenderRequest,
  type RenderResult,
  type WorkerReply,
  type WorkerRequest,
} from '@/lib/scene/types';
import { defineUnlistedScript } from 'wxt/utils/define-unlisted-script';

const MIN_NONCE_LENGTH = 16;
const STARTUP_TIMEOUT_MS = 15_000;      // compile + setup (the runtime allows 10 s) + the first frames
const FROZEN_MS = 4_000;                // no heartbeat for this long while visible: an endless loop
const WATCH_EVERY_MS = 500;
const POINTER_EVENTS = ['pointerdown', 'pointermove', 'pointerup', 'pointercancel', 'pointerleave'] as const;

interface InitMessage {
  type: 'init';
  nonce: string;
}

function isInit(value: unknown): value is InitMessage {
  return typeof value === 'object' && value !== null && (value as InitMessage).type === 'init'
    && typeof (value as InitMessage).nonce === 'string';
}

function isRenderRequest(value: unknown): value is RenderRequest {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return v.type === 'render' && typeof v.sceneId === 'string' && v.sceneId.length <= 64
    && typeof v.attempt === 'number' && isSceneKind(v.kind) && typeof v.code === 'string'
    && v.code.length <= MAX_SCENE_CODE_CHARS;
}

function randomToken(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join('');
}

function afterFrames(count: number): Promise<void> {
  return new Promise((resolve) => {
    const step = (left: number): void => {
      if (left <= 0) resolve();
      else requestAnimationFrame(() => step(left - 1));
    };
    step(count);
    setTimeout(resolve, 500);                     // frames are throttled when the tab is hidden
  });
}

export default defineUnlistedScript(() => {
  const expected = location.hash.slice(1);
  if (expected.length < MIN_NONCE_LENGTH) return;

  const stage = document.createElement('div');
  stage.id = 'stage';
  document.body.append(stage);

  let port: MessagePort | null = null;
  let teardown: (() => void) | null = null;

  const dispose = (): void => {
    teardown?.();
    teardown = null;
    stage.replaceChildren();
  };

  const stageSize = (): { width: number; height: number } => {
    const rect = stage.getBoundingClientRect();
    return { width: Math.max(1, Math.round(rect.width || innerWidth)), height: Math.max(1, Math.round(rect.height || innerHeight)) };
  };

  function runInWorker(request: RenderRequest & { kind: 'three' | 'canvas' }): Promise<RenderResult> {
    return new Promise((resolve) => {
      const base = { type: 'result' as const, sceneId: request.sceneId, attempt: request.attempt };
      const controller = new AbortController();
      const { signal } = controller;
      const token = randomToken();
      const canvas = document.createElement('canvas');
      canvas.className = 'cc-canvas';
      stage.replaceChildren(canvas);
      const worker = new SceneWorker({ name: 'clickytabs-scene' });
      let settled = false;
      let lastBeat = performance.now();

      const settle = (result: RenderResult): void => {
        if (settled) return;
        settled = true;
        clearTimeout(startup);
        resolve(result);
      };
      const lateError = (error: string): void => {
        port?.postMessage({ type: 'late-error', sceneId: request.sceneId, error });
      };
      const stop = (): void => {
        controller.abort();
        clearInterval(watch);
        clearTimeout(startup);
        worker.terminate();
      };
      teardown = () => {
        stop();
        settle({ ...base, ok: false, error: 'replaced by a newer scene' });   // its caller stops waiting
      };

      worker.onmessage = (event: MessageEvent<WorkerReply>) => {
        const message = event.data;
        if (!message || typeof message !== 'object' || message.token !== token) return;   // scene code can't forge it
        lastBeat = performance.now();
        switch (message.type) {
          case 'ready':
            settle({ ...base, ok: true, stats: message.stats });
            break;
          case 'error':
            if (settled) lateError(message.message);
            else settle({ ...base, ok: false, error: message.message });
            break;
          case 'cursor':
            if (/^[a-z-]{1,24}$/.test(message.value)) canvas.style.cursor = message.value;
            break;
          default:
            break;
        }
      };
      worker.onerror = (event: ErrorEvent) => {
        event.preventDefault();
        const error = `${event.message || 'the scene worker failed'}`;
        if (settled) lateError(error);
        else settle({ ...base, ok: false, error });
      };

      const watch = setInterval(() => {
        const now = performance.now();
        if (document.visibilityState !== 'visible') {
          lastBeat = now;                         // hidden tabs pause animation frames; that's not a hang
          return;
        }
        if (now - lastBeat > FROZEN_MS) {
          stop();
          const error = 'the scene stopped responding (an endless loop or a very slow frame) and was stopped';
          if (settled) lateError(error);
          else settle({ ...base, ok: false, error });
        }
      }, WATCH_EVERY_MS);
      const startup = setTimeout(() => {
        stop();
        settle({ ...base, ok: false, error: 'the scene did not finish starting within 15 s' });
      }, STARTUP_TIMEOUT_MS);

      // Input for OrbitControls / the 2D pointer, in canvas-relative CSS pixels.
      const forward = (event: Event): void => {
        const e = event as PointerEvent & WheelEvent & KeyboardEvent;
        const rect = canvas.getBoundingClientRect();
        const data: ForwardedEvent = { type: event.type };
        if ('clientX' in e && typeof e.clientX === 'number') {
          data.clientX = e.clientX - rect.left;
          data.clientY = e.clientY - rect.top;
          data.button = e.button;
          data.buttons = e.buttons;
          data.pointerId = e.pointerId;
          data.pointerType = e.pointerType;
        }
        if (event.type === 'wheel') {
          data.deltaX = e.deltaX;
          data.deltaY = e.deltaY;
          data.deltaMode = e.deltaMode;
        }
        if (event.type === 'keydown' || event.type === 'keyup') {
          data.key = e.key;
          data.code = e.code;
        }
        data.ctrlKey = e.ctrlKey;
        data.metaKey = e.metaKey;
        data.shiftKey = e.shiftKey;
        data.altKey = e.altKey;
        worker.postMessage({ type: 'event', event: data } satisfies WorkerRequest);
      };
      for (const type of POINTER_EVENTS) canvas.addEventListener(type, forward, { signal });
      canvas.addEventListener('wheel', (event) => {
        event.preventDefault();
        forward(event);
      }, { passive: false, signal });
      canvas.addEventListener('contextmenu', (event) => event.preventDefault(), { signal });
      addEventListener('keydown', forward, { signal });
      addEventListener('keyup', forward, { signal });
      const resize = new ResizeObserver(() => {
        const { width, height } = stageSize();
        worker.postMessage({ type: 'resize', width, height, dpr: devicePixelRatio } satisfies WorkerRequest);
      });
      resize.observe(stage);
      signal.addEventListener('abort', () => resize.disconnect());

      const { width, height } = stageSize();
      const offscreen = canvas.transferControlToOffscreen();
      const init: WorkerRequest = {
        type: 'init', token, kind: request.kind, canvas: offscreen, width, height, dpr: devicePixelRatio,
        code: request.code,
      };
      worker.postMessage(init, [offscreen]);
    });
  }

  async function showMarkup(request: RenderRequest & { kind: 'svg' | 'html' }): Promise<RenderResult> {
    const base = { type: 'result' as const, sceneId: request.sceneId, attempt: request.attempt };
    const { root, removed } = sanitizeScene(request.kind, request.code);
    const unsupported = removed.length ? ` (removed as unsupported: ${removed.join(', ')})` : '';
    if (!root) {
      return { ...base, ok: false, error: `there is nothing to show${request.kind === 'svg' ? ': no <svg> element' : ''}${unsupported}` };
    }
    const node = document.importNode(root, true);
    stage.replaceChildren(node);
    stage.dataset.kind = request.kind;
    await afterFrames(2);
    let visible = false;
    if (request.kind === 'svg') {
      const svg = node as SVGSVGElement;
      try {
        const box = svg.getBBox();
        visible = box.width * box.height > 0;
      } catch {
        visible = false;
      }
    } else {
      const rect = (node as HTMLElement).getBoundingClientRect();
      visible = rect.width * rect.height > 0 && ((node.textContent ?? '').trim().length > 0
        || (node as HTMLElement).querySelector('img, svg') !== null);
    }
    if (!visible) return { ...base, ok: false, error: `blank scene: nothing visible was drawn${unsupported}` };
    return { ...base, ok: true, stats: removed.length ? { removed } : {} };
  }

  async function render(request: RenderRequest): Promise<RenderResult> {
    dispose();
    delete stage.dataset.kind;
    try {
      if (request.kind === 'three' || request.kind === 'canvas') {
        stage.dataset.kind = request.kind;
        return await runInWorker(request as RenderRequest & { kind: 'three' | 'canvas' });
      }
      return await showMarkup(request as RenderRequest & { kind: 'svg' | 'html' });
    } catch (error) {
      return {
        type: 'result', sceneId: request.sceneId, attempt: request.attempt, ok: false,
        error: `the sandbox could not run the scene: ${error instanceof Error ? error.message : String(error)}`,
      };
    }
  }

  const onMessage = (event: MessageEvent): void => {
    if (event.source !== window.parent || !isInit(event.data)) return;
    if (event.data.nonce !== expected || event.ports.length !== 1) return;
    const received = event.ports[0];
    if (!received) return;
    window.removeEventListener('message', onMessage); // one init, ever
    port = received;
    received.onmessage = (message: MessageEvent) => {
      const data = message.data as { type?: unknown } | null;
      if (data?.type === 'ping') received.postMessage({ type: 'pong', at: Date.now() });
      else if (data?.type === 'dispose') dispose();
      else if (isRenderRequest(data)) void render(data).then((result) => received.postMessage(result));
    };
  };
  window.addEventListener('message', onMessage);
});
