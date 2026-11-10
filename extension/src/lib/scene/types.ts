/**
 * Messages of the scene pipeline.
 *
 *   content script (SceneHost) ──MessagePort──► sandbox page (scene-harness) ──postMessage──► Worker (scene runtime)
 *
 * The sandbox page and the Worker are separate realms that run model-written code (the Worker) or show sanitized
 * model-written markup (the page), so every message crossing into trusted code is shape-checked by the receiver.
 */

export const SCENE_KINDS = ['three', 'canvas', 'svg', 'html'] as const;
export type SceneKind = (typeof SCENE_KINDS)[number];

export const MAX_SCENE_CODE_CHARS = 60_000;

export function isSceneKind(value: unknown): value is SceneKind {
  return typeof value === 'string' && (SCENE_KINDS as readonly string[]).includes(value);
}

// ------------------------------------------------------------------------------- content script <-> sandbox page

export interface RenderRequest {
  type: 'render';
  sceneId: string;
  attempt: number;
  kind: SceneKind;
  code: string;
}

export type HarnessRequest = { type: 'ping' } | RenderRequest | { type: 'dispose' };

/** What rendering a scene showed: drawn objects (three), painted pixels (2D), elements kept (svg/html). */
export interface RenderStats {
  objects?: number;
  paintedRatio?: number;
  removed?: string[];
}

export interface RenderResult {
  type: 'result';
  sceneId: string;
  attempt: number;
  ok: boolean;
  error?: string;
  stats?: RenderStats;
}

/** A scene that rendered fine and failed later (a frame callback threw); shown on the stage, not repaired. */
export interface LateError {
  type: 'late-error';
  sceneId: string;
  error: string;
}

export type HarnessReply = { type: 'pong'; at: number } | RenderResult | LateError;

export function isRenderResult(value: unknown): value is RenderResult {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return v.type === 'result' && typeof v.sceneId === 'string' && typeof v.attempt === 'number'
    && typeof v.ok === 'boolean' && (v.error === undefined || typeof v.error === 'string');
}

export function isLateError(value: unknown): value is LateError {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return v.type === 'late-error' && typeof v.sceneId === 'string' && typeof v.error === 'string';
}

// ---------------------------------------------------------------------------------- sandbox page <-> Worker

export interface ForwardedEvent {
  type: string;
  pointerId?: number;
  pointerType?: string;
  button?: number;
  buttons?: number;
  clientX?: number;
  clientY?: number;
  deltaX?: number;
  deltaY?: number;
  deltaMode?: number;
  ctrlKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
  key?: string;
  code?: string;
}

export type WorkerRequest =
  | {
      type: 'init';
      token: string;
      kind: 'three' | 'canvas';
      canvas: OffscreenCanvas;
      width: number;
      height: number;
      dpr: number;
      code: string;
    }
  | { type: 'resize'; width: number; height: number; dpr: number }
  | { type: 'event'; event: ForwardedEvent };

/** Every runtime message carries the init token: scene code can call postMessage too, but never learns it. */
export type WorkerReply =
  | { type: 'heartbeat'; token: string }
  | { type: 'ready'; token: string; stats: RenderStats }
  | { type: 'error'; token: string; phase: 'compile' | 'setup' | 'frame' | 'blank'; message: string }
  | { type: 'cursor'; token: string; value: string };
