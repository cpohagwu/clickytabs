/**
 * Hosts the sandboxed scene page (public/scene.html) inside the companion's closed shadow root.
 *
 * Handshake (the page cannot drive it): the iframe URL carries a random nonce in its hash; after load we post ONE
 * `{type:"init", nonce}` message that transfers a MessagePort, and from then on talk only over that port. The
 * harness accepts exactly one init whose nonce matches its own hash and that comes from its parent window, so
 * another page embedding scene.html (or a script posting to it) gets nothing. We never listen to window messages.
 *
 * Over the port: `ping` -> `pong`; `render` -> one `result`; `late-error` whenever a shown scene breaks.
 */
import { APP_NAME } from './brand';
import type { ScenePingReply } from './messages';
import { isLateError, isRenderResult, type RenderRequest, type RenderResult } from './scene/types';

export const SCENE_LOAD_TIMEOUT_MS = 5_000;
export const SCENE_REPLY_TIMEOUT_MS = 3_000;
/** The harness answers a render within ~15 s (its own startup limit); this is the backstop. */
export const SCENE_RENDER_TIMEOUT_MS = 25_000;

export interface ScenePong {
  type: 'pong';
  at: number;
}

export function randomNonce(bytes = 16): string {
  const values = crypto.getRandomValues(new Uint8Array(bytes));
  return Array.from(values, (v) => v.toString(16).padStart(2, '0')).join('');
}

export interface SceneHostOptions {
  /** A visible stage frame (the scene panel) rather than the hidden probe frame. */
  visible?: boolean;
  onLateError?: (sceneId: string, error: string) => void;
}

export class SceneHost {
  private iframe: HTMLIFrameElement | null = null;
  private port: Promise<MessagePort> | null = null;
  private readonly waiters = new Map<string, { resolve: (value: unknown) => void; reject: (error: Error) => void }>();

  constructor(
    private readonly container: Element | ShadowRoot,
    private readonly sceneUrl: () => string,
    private readonly options: SceneHostOptions = {},
  ) {}

  private open(): Promise<MessagePort> {
    this.port ??= new Promise<MessagePort>((resolve, reject) => {
      const nonce = randomNonce();
      const iframe = document.createElement('iframe');
      iframe.setAttribute('sandbox', 'allow-scripts');
      if (!this.options.visible) iframe.setAttribute('aria-hidden', 'true');
      iframe.setAttribute('title', this.options.visible ? 'AI-generated scene' : `${APP_NAME} scene`);
      iframe.className = 'cc-scene-frame';
      const timer = setTimeout(() => reject(new Error('the scene sandbox did not load')), SCENE_LOAD_TIMEOUT_MS);
      iframe.addEventListener(
        'load',
        () => {
          clearTimeout(timer);
          const target = iframe.contentWindow;
          if (!target) {
            reject(new Error('the scene sandbox has no window'));
            return;
          }
          const channel = new MessageChannel();
          channel.port1.onmessage = (event: MessageEvent) => this.dispatch(event.data);
          target.postMessage({ type: 'init', nonce }, '*', [channel.port2]);
          resolve(channel.port1);
        },
        { once: true },
      );
      iframe.src = `${this.sceneUrl()}#${nonce}`;
      this.container.append(iframe);
      this.iframe = iframe;
    }).catch((error: unknown) => {
      this.destroy();
      throw error;
    });
    return this.port;
  }

  private dispatch(data: unknown): void {
    if (isRenderResult(data)) {
      this.resolveWaiter(`result:${data.sceneId}:${data.attempt}`, data);
    } else if (isLateError(data)) {
      this.options.onLateError?.(data.sceneId, data.error.slice(0, 500));
    } else if (typeof data === 'object' && data !== null && (data as { type?: unknown }).type === 'pong') {
      this.resolveWaiter('pong', data);
    }
  }

  private resolveWaiter(key: string, value: unknown): void {
    const waiter = this.waiters.get(key);
    if (!waiter) return;
    this.waiters.delete(key);
    waiter.resolve(value);
  }

  private request<T>(key: string, message: unknown, timeoutMs: number, timeoutError: string): Promise<T> {
    return this.open().then((port) => new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiters.delete(key);
        reject(new Error(timeoutError));
      }, timeoutMs);
      this.waiters.set(key, {
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value as T);
        },
        reject: (error) => {
          clearTimeout(timer);
          reject(error);
        },
      });
      port.postMessage(message);
    }));
  }

  async ping(): Promise<ScenePingReply> {
    const started = performance.now();
    try {
      const pong = await this.request<ScenePong>('pong', { type: 'ping' }, SCENE_REPLY_TIMEOUT_MS,
        'the scene sandbox did not answer');
      return { ok: true, pong, rttMs: Math.round(performance.now() - started) };
    } catch (error) {
      return { ok: false, error: error instanceof Error ? error.message : String(error) };
    }
  }

  /** Render a scene; never rejects (a failure is a result with ok=false). */
  async render(request: Omit<RenderRequest, 'type'>): Promise<RenderResult> {
    const key = `result:${request.sceneId}:${request.attempt}`;
    try {
      return await this.request<RenderResult>(key, { type: 'render', ...request }, SCENE_RENDER_TIMEOUT_MS,
        'the scene sandbox did not answer');
    } catch (error) {
      return {
        type: 'result', sceneId: request.sceneId, attempt: request.attempt, ok: false,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }

  /** Removes the frame (its Worker dies with it); pending requests fail at once instead of timing out. */
  destroy(): void {
    this.iframe?.remove();
    this.iframe = null;
    this.port = null;
    const waiting = [...this.waiters.values()];
    this.waiters.clear();
    for (const waiter of waiting) waiter.reject(new Error('the stage was closed'));
  }
}
