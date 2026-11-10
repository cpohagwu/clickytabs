/**
 * Speech-synchronised directive timing (pure; timers injected).
 *
 * A spoken segment carries directives tagged with `at_char`, the offset in its text where the brain emitted them.
 * Without TTS alignment the directive fires at `start + duration × at_char / text.length`, so the pointer lands as
 * the voice says "this button". With alignment (ElevenLabs) the exact character time replaces the estimate.
 */

export interface TimedItem<T> {
  at_char: number;
  item: T;
}

export interface Scheduled<T> {
  delayMs: number;
  item: T;
}

/** Milliseconds into the segment at which character `atChar` is spoken (estimated linearly). */
export function offsetForChar(atChar: number, textLength: number, durationMs: number): number {
  if (textLength <= 0 || durationMs <= 0) return 0;
  return Math.round((Math.min(Math.max(atChar, 0), textLength) / textLength) * durationMs);
}

/**
 * Delays for every item of a segment, in firing order. `charTimes` (from TTS alignment) wins over the estimate
 * when it covers the character.
 */
export function scheduleSegment<T>(
  text: string,
  durationMs: number,
  items: readonly TimedItem<T>[],
  charTimes?: readonly number[],
): Scheduled<T>[] {
  return items
    .map((entry, index) => ({
      index,
      delayMs: charTimes?.[entry.at_char] ?? offsetForChar(entry.at_char, text.length, durationMs),
      item: entry.item,
    }))
    .sort((a, b) => a.delayMs - b.delayMs || a.index - b.index)
    .map(({ delayMs, item }) => ({ delayMs, item }));
}

export interface TimelineTimers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

/** A cancellable set of pending callbacks (one per turn or demo run). */
export class Timeline {
  private readonly handles = new Set<unknown>();
  private cancelled = false;

  constructor(
    private readonly timers: TimelineTimers = {
      setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
      clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
    },
  ) {}

  get pending(): number {
    return this.handles.size;
  }

  get isCancelled(): boolean {
    return this.cancelled;
  }

  at(delayMs: number, fn: () => void): void {
    if (this.cancelled) return;
    const handle = this.timers.setTimeout(() => {
      this.handles.delete(handle);
      if (!this.cancelled) fn();
    }, Math.max(0, delayMs));
    this.handles.add(handle);
  }

  /** Resolves after `ms` unless cancelled first (then it never resolves; callers check isCancelled). */
  sleep(ms: number): Promise<void> {
    return new Promise((resolve) => this.at(ms, resolve));
  }

  cancel(): void {
    this.cancelled = true;
    for (const handle of this.handles) this.timers.clearTimeout(handle);
    this.handles.clear();
  }
}
