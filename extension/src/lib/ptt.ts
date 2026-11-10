/**
 * Push-to-talk ownership, held by the service worker (pure: timers and effects are injected).
 *
 * Frames only report raw chord down/up. The worker decides, because a lost keyup must never leave the mic open:
 * a hold ends on a keyup from ANY frame of the tab, on frame blur / hidden / pagehide, when the tab loses focus
 * (tab switch, window blur), closes or navigates, on the `toggle-listen` command, and after {@link MAX_HOLD_MS}.
 * Only one tab can hold at a time (there is one microphone).
 */
import type { FrameReleaseReason } from './messages';

export const MAX_HOLD_MS = 60_000;

export type PttReleaseReason =
  | FrameReleaseReason
  | 'tab-switch'
  | 'window-blur'
  | 'tab-closed'
  | 'navigated'
  | 'max-hold'
  | 'command'
  | 'superseded';

export interface PttEffects {
  /** A hold started: show the recording indicator (badge + orb), later start the recorder. */
  started(tabId: number): void;
  /** A hold ended: clear the indicator, later stop the recorder and send `audio.end`. */
  stopped(tabId: number, reason: PttReleaseReason, heldMs: number): void;
}

export interface PttTimers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

interface Hold {
  tabId: number;
  startedAt: number;
  watchdog: unknown;
}

export class PttController {
  private hold: Hold | null = null;

  constructor(
    private readonly effects: PttEffects,
    private readonly timers: PttTimers = {
      setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
      clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
    },
    private readonly now: () => number = Date.now,
  ) {}

  get holdingTab(): number | null {
    return this.hold?.tabId ?? null;
  }

  isHolding(tabId: number): boolean {
    return this.hold?.tabId === tabId;
  }

  /** Chord pressed in `tabId`. Returns true when a new hold started. */
  down(tabId: number): boolean {
    if (this.hold?.tabId === tabId) return false; // key repeat or a second frame: already holding
    if (this.hold) this.end('superseded');
    const watchdog = this.timers.setTimeout(() => {
      if (this.hold?.tabId === tabId) this.end('max-hold');
    }, MAX_HOLD_MS);
    this.hold = { tabId, startedAt: this.now(), watchdog };
    this.effects.started(tabId);
    return true;
  }

  /** Chord released (or a frame lost focus) in `tabId`. Returns true when that ended the hold. */
  up(tabId: number, reason: PttReleaseReason): boolean {
    if (this.hold?.tabId !== tabId) return false;
    this.end(reason);
    return true;
  }

  /** `toggle-listen` command: start in `tabId`, or stop if it is already holding. */
  toggle(tabId: number): boolean {
    if (this.hold?.tabId === tabId) {
      this.end('command');
      return false;
    }
    return this.down(tabId);
  }

  /** Browser-level events: focus moved, tab closed or navigating. */
  releaseTab(tabId: number, reason: PttReleaseReason): void {
    if (this.hold?.tabId === tabId) this.end(reason);
  }

  /** The active tab changed (or the window lost focus): any hold outside `activeTabId` ends. */
  focusChanged(activeTabId: number | null, reason: PttReleaseReason): void {
    if (this.hold && this.hold.tabId !== activeTabId) this.end(reason);
  }

  private end(reason: PttReleaseReason): void {
    const hold = this.hold;
    if (!hold) return;
    this.hold = null;
    this.timers.clearTimeout(hold.watchdog);
    this.effects.stopped(hold.tabId, reason, this.now() - hold.startedAt);
  }
}
