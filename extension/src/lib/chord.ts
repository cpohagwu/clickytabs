/**
 * Push-to-talk chord detection inside one frame (pure: timers and callbacks are injected).
 *
 * - `hold-right-ctrl` (default): Right Ctrl held alone for {@link ARM_MS}. Any other key or a mouse press during the
 *   hold cancels it (so Ctrl+C or Ctrl+click never record). Modifier-only, so nothing is ever preventDefault'ed.
 * - `hold-ctrl-space`: Ctrl+Space starts at once and ends when either key is released. Skipped inside code editors
 *   (Monaco / CodeMirror use Ctrl+Space for autocomplete); Space is preventDefault'ed only while it is ours.
 */
import type { FrameReleaseReason } from './messages';
import type { PttChord } from './prefs';

export const ARM_MS = 200;

export interface KeyLike {
  code: string;
  key: string;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  metaKey: boolean;
  repeat: boolean;
}

export interface ChordCallbacks {
  down(): void;
  up(reason: FrameReleaseReason): void;
}

export interface ChordTimers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

type Phase = 'idle' | 'arming' | 'holding';

/** What the frame should do with the event. */
export type KeyVerdict = 'ignore' | 'prevent';

export class ChordDetector {
  private phase: Phase = 'idle';
  private armTimer: unknown = null;

  constructor(
    private chord: PttChord,
    private readonly callbacks: ChordCallbacks,
    private readonly timers: ChordTimers = {
      setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
      clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
    },
  ) {}

  get holding(): boolean {
    return this.phase === 'holding';
  }

  setChord(chord: PttChord): void {
    if (chord === this.chord) return;
    this.release('cancel');
    this.chord = chord;
  }

  keydown(event: KeyLike, inCodeEditor = false): KeyVerdict {
    if (this.chord === 'hold-right-ctrl') return this.rightCtrlDown(event);
    return this.ctrlSpaceDown(event, inCodeEditor);
  }

  keyup(event: KeyLike): KeyVerdict {
    if (this.chord === 'hold-right-ctrl') {
      if (event.code === 'ControlRight') this.release('keyup');
      return 'ignore';
    }
    if (this.phase === 'holding' && (event.code === 'Space' || event.key === 'Control')) {
      this.release('keyup');
      return event.code === 'Space' ? 'prevent' : 'ignore';
    }
    return 'ignore';
  }

  /** Any mouse press while the chord is down means the user is Ctrl+clicking, not talking. */
  mousedown(): void {
    if (this.chord === 'hold-right-ctrl') this.release('cancel');
  }

  /** Frame lost focus, became hidden or is being unloaded. */
  release(reason: FrameReleaseReason): void {
    if (this.armTimer !== null) {
      this.timers.clearTimeout(this.armTimer);
      this.armTimer = null;
    }
    const wasHolding = this.phase === 'holding';
    this.phase = 'idle';
    if (wasHolding) this.callbacks.up(reason);
  }

  private rightCtrlDown(event: KeyLike): KeyVerdict {
    if (event.code === 'ControlRight') {
      if (event.repeat || this.phase !== 'idle') return 'ignore';
      if (event.altKey || event.shiftKey || event.metaKey) return 'ignore';
      this.phase = 'arming';
      this.armTimer = this.timers.setTimeout(() => {
        this.armTimer = null;
        if (this.phase !== 'arming') return;
        this.phase = 'holding';
        this.callbacks.down();
      }, ARM_MS);
      return 'ignore';
    }
    if (this.phase !== 'idle') this.release('cancel'); // a shortcut such as Ctrl+C, not push-to-talk
    return 'ignore';
  }

  private ctrlSpaceDown(event: KeyLike, inCodeEditor: boolean): KeyVerdict {
    const isChord = event.code === 'Space' && event.ctrlKey && !event.altKey && !event.metaKey && !event.shiftKey;
    if (this.phase === 'holding') {
      if (event.code === 'Space') return 'prevent'; // auto-repeat while held
      if (event.key !== 'Control') this.release('cancel');
      return 'ignore';
    }
    if (!isChord || inCodeEditor || event.repeat) return 'ignore';
    this.phase = 'holding';
    this.callbacks.down();
    return 'prevent';
  }
}

/** True when `element` sits inside a code editor that owns Ctrl+Space. */
export function isInCodeEditor(element: Element | null): boolean {
  return element?.closest('.monaco-editor, .cm-editor, .CodeMirror') != null;
}
