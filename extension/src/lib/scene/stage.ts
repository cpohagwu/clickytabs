/**
 * The scene stage on the page side: the backend's scene.building / scene.render / scene.failed events become the
 * ScenePanel's state; renders go to the sandbox (SceneHost) and their results back to the worker as scene.result.
 *
 * One stage per tab, one scene at a time: a newer scene replaces the one showing. Closing the stage while a scene is
 * still being written turns that scene down (its render is answered `unavailable`, so the backend doesn't repair it).
 */
import { writable, type Readable } from 'svelte/store';

import type { OpenScene, SceneBuilding, SceneFailed, SceneRender } from '../protocol.gen';
import { SceneHost } from '../scene-host';
import type { RenderResult, RenderStats, SceneKind } from './types';

export type StageStatus = 'building' | 'rendering' | 'shown' | 'repairing' | 'failed';

export interface StageState {
  open: boolean;
  sceneId: string | null;
  kind: SceneKind | null;
  title: string;
  status: StageStatus;
  error: string | null;
  attempt: number;
  docked: boolean;
  /** Date.now() when the backend started writing the scene, and how long the codegen model took. */
  startedAt: number;
  codegenMs: number | null;
  stats: RenderStats | null;
}

export const CLOSED_STAGE: StageState = {
  open: false, sceneId: null, kind: null, title: '', status: 'building', error: null, attempt: 0, docked: false,
  startedAt: 0, codegenMs: null, stats: null,
};

export type SceneEvent = SceneBuilding | SceneRender | SceneFailed;

export interface SceneResultMessage {
  scene_id: string;
  attempt: number;
  ok: boolean;
  error?: string;
  unavailable?: boolean;
}

export interface StageDeps {
  sendResult: (result: SceneResultMessage) => void;
  sceneUrl: () => string;
  now?: () => number;
}

/** What tests and the debug state see of the stage. */
export interface StageDebug {
  open: boolean;
  sceneId: string | null;
  kind: SceneKind | null;
  title: string;
  status: StageStatus;
  attempt: number;
  error: string | null;
  docked: boolean;
  codegenMs: number | null;
  stats: RenderStats | null;
}

const MAX_DISMISSED = 16;
/** A scene still being written or rendered after this long has lost its backend (restart, dropped connection). */
export const STAGE_STALL_MS = 6 * 60_000;

/** The stage's state; one stage per content script, so one module-level store (the companion store does the same). */
export const stageStore = writable<StageState>(CLOSED_STAGE);

export class StageController {
  private readonly store = stageStore;
  private state: StageState = CLOSED_STAGE;
  private host: SceneHost | null = null;
  private pending: SceneRender | null = null;
  private readonly dismissed: string[] = [];
  private stall: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly deps: StageDeps) {}

  get stage(): Readable<StageState> {
    return this.store;
  }

  private set(patch: Partial<StageState>): void {
    this.state = { ...this.state, ...patch };
    this.store.set(this.state);
    this.watchStall();
  }

  /** Waiting states get a deadline, renewed by every change; a stalled scene ends as failed. */
  private watchStall(): void {
    if (this.stall !== null) clearTimeout(this.stall);
    this.stall = null;
    const { open, status, sceneId } = this.state;
    if (!open || status === 'shown' || status === 'failed') return;
    this.stall = setTimeout(() => {
      if (this.state.sceneId === sceneId && this.state.status !== 'shown' && this.state.status !== 'failed') {
        this.set({ status: 'failed', error: 'No answer about this scene: the backend may have restarted. Ask again.' });
      }
    }, STAGE_STALL_MS);
  }

  private now(): number {
    return this.deps.now?.() ?? Date.now();
  }

  // ------------------------------------------------------------------------------------------------ panel

  /** The panel mounted its body: scenes render into a sandbox frame there. */
  attachBody(body: HTMLElement): void {
    this.host?.destroy();
    this.host = new SceneHost(body, this.deps.sceneUrl, {
      visible: true,
      onLateError: (sceneId, error) => {
        if (sceneId === this.state.sceneId) this.set({ error: `The scene stopped: ${error}` });
      },
    });
    const pending = this.pending;
    this.pending = null;
    if (pending) void this.run(pending);
  }

  detachBody(): void {
    this.host?.destroy();
    this.host = null;
  }

  close(): void {
    const { sceneId, status } = this.state;
    if (sceneId && status !== 'shown' && status !== 'failed') this.dismiss(sceneId);
    if (this.pending) {
      this.deps.sendResult({ scene_id: this.pending.scene_id, attempt: this.pending.attempt ?? 1, ok: false,
        unavailable: true, error: 'the user closed the stage' });
      this.pending = null;
    }
    this.detachBody();
    this.set({ ...CLOSED_STAGE, docked: this.state.docked });
  }

  /**
   * The backend connection dropped: a scene still being written or repaired is gone with its session (the backend
   * cancels a session's scene job when the socket closes), so say so now rather than waiting for the stall timer.
   */
  connectionLost(): void {
    const { open, status } = this.state;
    if (open && (status === 'building' || status === 'repairing')) {
      this.set({ status: 'failed', error: 'Lost the connection to the backend while the scene was being written. Ask again.' });
    }
  }

  toggleDock(): void {
    this.set({ docked: !this.state.docked });
  }

  private dismiss(sceneId: string): void {
    this.dismissed.push(sceneId);
    if (this.dismissed.length > MAX_DISMISSED) this.dismissed.shift();
  }

  // ------------------------------------------------------------------------------------------------ events

  onEvent(event: SceneEvent): void {
    if (this.dismissed.includes(event.scene_id)) {
      if (event.type === 'scene.render') {
        this.deps.sendResult({ scene_id: event.scene_id, attempt: event.attempt ?? 1, ok: false, unavailable: true,
          error: 'the user closed the stage' });
      }
      return;
    }
    switch (event.type) {
      case 'scene.building':
        this.pending = null;
        this.set({
          open: true, sceneId: event.scene_id, kind: event.kind, title: event.title ?? '', status: 'building',
          error: null, attempt: 0, startedAt: this.now(), codegenMs: null, stats: null,
        });
        return;
      case 'scene.render': {
        const attempt = event.attempt ?? 1;
        const fresh = event.scene_id !== this.state.sceneId;
        this.set({
          open: true, sceneId: event.scene_id, kind: event.kind, title: event.title ?? this.state.title,
          status: attempt > 1 ? 'repairing' : 'rendering', error: null, attempt, codegenMs: event.codegen_ms ?? null,
          ...(fresh ? { startedAt: this.now(), stats: null } : {}),
        });
        if (this.host) void this.run(event);
        else this.pending = event;           // the panel mounts on this store update, then attachBody runs it
        return;
      }
      case 'scene.failed':
        if (event.scene_id === this.state.sceneId) this.set({ open: true, status: 'failed', error: event.error });
        return;
    }
  }

  private async run(event: SceneRender): Promise<void> {
    const host = this.host;
    if (!host) return;
    const attempt = event.attempt ?? 1;
    const result: RenderResult = await host.render({
      sceneId: event.scene_id, attempt, kind: event.kind, code: event.code,
    });
    if (this.host !== host) {                                   // closed while rendering: no repair for this one
      this.deps.sendResult({ scene_id: event.scene_id, attempt, ok: false, unavailable: true,
        error: 'the user closed the stage' });
      return;
    }
    this.deps.sendResult({
      scene_id: event.scene_id, attempt, ok: result.ok, ...(result.error ? { error: result.error } : {}),
    });
    if (event.scene_id !== this.state.sceneId) return;           // a newer scene took the stage meanwhile
    if (result.ok) this.set({ status: 'shown', error: null, stats: result.stats ?? null });
    else this.set({ status: 'repairing', error: result.error ?? 'the scene did not render', stats: null });
  }

  /** Render code straight into the stage, without the backend (debug.sceneRender: e2e tests of the sandbox). */
  async renderDirect(kind: SceneKind, code: string, title = 'Debug scene'): Promise<RenderResult> {
    const sceneId = `debug${crypto.getRandomValues(new Uint32Array(2)).join('')}`;
    this.set({
      open: true, sceneId, kind, title, status: 'rendering', error: null, attempt: 1, startedAt: this.now(),
      codegenMs: null, stats: null,
    });
    for (let i = 0; i < 50 && !this.host; i += 1) await new Promise((r) => setTimeout(r, 20));   // panel mounting
    if (!this.host) return { type: 'result', sceneId, attempt: 1, ok: false, error: 'the stage did not mount' };
    const result = await this.host.render({ sceneId, attempt: 1, kind, code });
    if (sceneId === this.state.sceneId) {
      this.set(result.ok ? { status: 'shown', stats: result.stats ?? null }
        : { status: 'failed', error: result.error ?? 'the scene did not render' });
    }
    return result;
  }

  /** The scene in front of the page, for the next turn's page context (so "make it bigger" can edit it). */
  openScene(): OpenScene | null {
    const { open, sceneId, status, title } = this.state;
    if (!open || !sceneId || status !== 'shown' || sceneId.startsWith('debug')) return null;
    return { scene_id: sceneId, title: title.slice(0, 200) };
  }

  debug(): StageDebug {
    const { open, sceneId, kind, title, status, attempt, error, docked, codegenMs, stats } = this.state;
    return { open, sceneId, kind, title, status, attempt, error, docked, codegenMs, stats };
  }

  destroy(): void {
    this.detachBody();
    this.pending = null;
    if (this.stall !== null) clearTimeout(this.stall);
  }
}
