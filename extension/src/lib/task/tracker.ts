/**
 * The browser-task tracker on the page side: consent, start, steps (with a small screenshot), the agent's
 * questions before irreversible steps, and the result. The worker routes a task's events only to the tab that asked
 * for it; answers go back through the worker, which checks this tab owns the task.
 */
import { writable } from 'svelte/store';

import type { TaskConfirmRequest, TaskDone, TaskStarted, TaskStep } from '../protocol.gen';

export type TrackerStatus = 'consent' | 'starting' | 'running' | 'confirm' | 'done' | 'failed';

/** What the worker tells the tab when it asks for consent (sites as the worker worked them out). */
export interface TaskConsentView {
  type: 'task.consent';
  task_id: string;
  goal: string;
  sites: string[];
  cross_site: boolean;
  session: boolean;
  cookies: number;
  /** Allowed without asking ("Always" for this site), or waiting for the consent window. */
  auto: boolean;
}

export type TaskEvent = TaskConsentView | TaskStarted | TaskStep | TaskConfirmRequest | TaskDone;

export interface TrackerStep {
  n: number;
  text: string;
  url: string | null;
}

export interface TrackerState {
  open: boolean;
  taskId: string | null;
  goal: string;
  status: TrackerStatus;
  sites: string[];
  session: boolean;
  cookies: number;
  auto: boolean;
  steps: TrackerStep[];
  thumb: string | null;
  confirm: { requestId: string; question: string } | null;
  summary: string;
  finalUrl: string | null;
}

export const CLOSED_TRACKER: TrackerState = {
  open: false, taskId: null, goal: '', status: 'consent', sites: [], session: false, cookies: 0, auto: false,
  steps: [], thumb: null, confirm: null, summary: '', finalUrl: null,
};

export const MAX_STEPS_SHOWN = 6;

/** One tracker per content script, so one module-level store (as the companion and the stage do). */
export const trackerStore = writable<TrackerState>(CLOSED_TRACKER);

export interface TrackerDeps {
  stop: (taskId: string) => void;
  answer: (taskId: string, requestId: string, approved: boolean) => void;
  open: (taskId: string, url: string) => void;
}

export class TrackerController {
  private state: TrackerState = CLOSED_TRACKER;

  constructor(private readonly deps: TrackerDeps) {}

  private set(patch: Partial<TrackerState>): void {
    this.state = { ...this.state, ...patch };
    trackerStore.set(this.state);
  }

  onEvent(event: TaskEvent): void {
    const current = event.task_id === this.state.taskId;
    switch (event.type) {
      case 'task.consent':
        this.set({
          ...CLOSED_TRACKER, open: true, taskId: event.task_id, goal: event.goal, status: event.auto ? 'starting' : 'consent',
          sites: event.sites, session: event.session, cookies: event.cookies, auto: event.auto,
        });
        return;
      case 'task.started':
        if (!current) return;
        this.set({ status: 'starting', sites: event.sites ?? this.state.sites, cookies: event.cookies ?? 0 });
        return;
      case 'task.step': {
        if (!current) return;
        const steps = [...this.state.steps, { n: event.n, text: event.action_text, url: event.url ?? null }];
        this.set({ status: this.state.confirm ? 'confirm' : 'running', steps: steps.slice(-MAX_STEPS_SHOWN),
          thumb: event.thumb_b64 ?? this.state.thumb });
        return;
      }
      case 'task.confirm.request':
        if (!current) return;
        this.set({ status: 'confirm', confirm: { requestId: event.request_id, question: event.question } });
        return;
      case 'task.done':
        if (!current && this.state.open && this.state.status !== 'done' && this.state.status !== 'failed') return;
        this.set({
          open: true, taskId: event.task_id, status: event.success ? 'done' : 'failed', confirm: null,
          summary: event.summary, finalUrl: event.final_url ?? null,
          goal: current ? this.state.goal : this.state.goal || 'Browser task',
        });
        return;
    }
  }

  stop(): void {
    const { taskId, status } = this.state;
    if (taskId && status !== 'done' && status !== 'failed') this.deps.stop(taskId);
  }

  answer(approved: boolean): void {
    const { taskId, confirm } = this.state;
    if (!taskId || !confirm) return;
    this.deps.answer(taskId, confirm.requestId, approved);
    this.set({ status: 'running', confirm: null });
  }

  openInTab(): void {
    const { taskId, finalUrl } = this.state;
    if (taskId && finalUrl) this.deps.open(taskId, finalUrl);
  }

  close(): void {
    const { status } = this.state;
    if (status === 'done' || status === 'failed') this.set(CLOSED_TRACKER);
  }

  /**
   * A new service worker started: the old one's backend session closed with it, and the backend stops a
   * session's task (and its browser) when the session ends. Say so instead of showing a task that never finishes.
   */
  workerRestarted(): void {
    const { open, status } = this.state;
    if (!open || status === 'done' || status === 'failed') return;
    this.set({
      status: 'failed', confirm: null, finalUrl: null,
      summary: 'Stopped: the connection to the backend was interrupted, so the task was ended. Ask again to retry.',
    });
  }

  debug(): Omit<TrackerState, 'thumb'> & { hasThumb: boolean } {
    const { thumb, ...rest } = this.state;
    return { ...rest, hasThumb: thumb !== null };
  }
}
