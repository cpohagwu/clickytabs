import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ARM_MS, ChordDetector, type KeyLike } from '@/lib/chord';
import { MAX_HOLD_MS, PttController, type PttReleaseReason } from '@/lib/ptt';

const key = (code: string, extra: Partial<KeyLike> = {}): KeyLike => ({
  code,
  key: code.startsWith('Control') ? 'Control' : code === 'Space' ? ' ' : code.replace('Key', '').toLowerCase(),
  ctrlKey: code.startsWith('Control'),
  altKey: false,
  shiftKey: false,
  metaKey: false,
  repeat: false,
  ...extra,
});

describe('PttController (service worker)', () => {
  let events: string[];
  let ptt: PttController;

  beforeEach(() => {
    vi.useFakeTimers();
    events = [];
    ptt = new PttController({
      started: (tab) => events.push(`start:${tab}`),
      stopped: (tab, reason: PttReleaseReason) => events.push(`stop:${tab}:${reason}`),
    });
  });
  afterEach(() => vi.useRealTimers());

  it('starts once per tab and stops on keyup from any frame', () => {
    expect(ptt.down(1)).toBe(true);
    expect(ptt.down(1)).toBe(false);
    expect(ptt.up(1, 'keyup')).toBe(true);
    expect(events).toEqual(['start:1', 'stop:1:keyup']);
  });

  it('ignores releases for other tabs', () => {
    ptt.down(1);
    expect(ptt.up(2, 'keyup')).toBe(false);
    expect(ptt.isHolding(1)).toBe(true);
  });

  it('ends a hold whose keyup was lost after MAX_HOLD_MS', () => {
    ptt.down(7);
    vi.advanceTimersByTime(MAX_HOLD_MS - 1);
    expect(ptt.isHolding(7)).toBe(true);
    vi.advanceTimersByTime(1);
    expect(events).toEqual(['start:7', 'stop:7:max-hold']);
  });

  it('releases on tab switch, window blur, close and navigation', () => {
    ptt.down(1);
    ptt.focusChanged(2, 'tab-switch');
    ptt.down(3);
    ptt.focusChanged(null, 'window-blur');
    ptt.down(4);
    ptt.releaseTab(4, 'navigated');
    ptt.down(5);
    ptt.releaseTab(5, 'tab-closed');
    expect(events).toEqual([
      'start:1', 'stop:1:tab-switch', 'start:3', 'stop:3:window-blur',
      'start:4', 'stop:4:navigated', 'start:5', 'stop:5:tab-closed',
    ]);
  });

  it('keeps the hold when focus stays on the holding tab', () => {
    ptt.down(1);
    ptt.focusChanged(1, 'tab-switch');
    expect(ptt.isHolding(1)).toBe(true);
  });

  it('allows one microphone at a time and supports the toggle command', () => {
    ptt.down(1);
    ptt.down(2);
    expect(ptt.toggle(2)).toBe(false);
    expect(ptt.toggle(2)).toBe(true);
    expect(events).toEqual(['start:1', 'stop:1:superseded', 'start:2', 'stop:2:command', 'start:2']);
  });

  it('clears the watchdog when a hold ends normally', () => {
    ptt.down(1);
    ptt.up(1, 'keyup');
    vi.advanceTimersByTime(MAX_HOLD_MS * 2);
    expect(events).toEqual(['start:1', 'stop:1:keyup']);
  });
});

describe('ChordDetector (frame)', () => {
  let events: string[];
  let detector: ChordDetector;

  beforeEach(() => {
    vi.useFakeTimers();
    events = [];
    detector = new ChordDetector('hold-right-ctrl', {
      down: () => events.push('down'),
      up: (reason) => events.push(`up:${reason}`),
    });
  });
  afterEach(() => vi.useRealTimers());

  it('arms Right Ctrl after ARM_MS and never prevents default', () => {
    expect(detector.keydown(key('ControlRight'))).toBe('ignore');
    vi.advanceTimersByTime(ARM_MS);
    expect(events).toEqual(['down']);
    expect(detector.keyup(key('ControlRight'))).toBe('ignore');
    expect(events).toEqual(['down', 'up:keyup']);
  });

  it('a quick Right Ctrl tap does nothing', () => {
    detector.keydown(key('ControlRight'));
    detector.keyup(key('ControlRight'));
    vi.advanceTimersByTime(ARM_MS * 2);
    expect(events).toEqual([]);
  });

  it('Ctrl+C (another key during the arm window) cancels without starting', () => {
    detector.keydown(key('ControlRight'));
    detector.keydown(key('KeyC', { ctrlKey: true }));
    vi.advanceTimersByTime(ARM_MS * 2);
    expect(events).toEqual([]);
  });

  it('another key or a Ctrl+click during the hold cancels it', () => {
    detector.keydown(key('ControlRight'));
    vi.advanceTimersByTime(ARM_MS);
    detector.keydown(key('KeyV', { ctrlKey: true }));
    detector.keydown(key('ControlRight'));
    vi.advanceTimersByTime(ARM_MS);
    detector.mousedown();
    expect(events).toEqual(['down', 'up:cancel', 'down', 'up:cancel']);
  });

  it('ignores Left Ctrl, key repeat and modifier combos', () => {
    detector.keydown(key('ControlLeft'));
    detector.keydown(key('ControlRight', { shiftKey: true }));
    vi.advanceTimersByTime(ARM_MS);
    expect(events).toEqual([]);
    detector.keydown(key('ControlRight'));
    vi.advanceTimersByTime(ARM_MS);
    detector.keydown(key('ControlRight', { repeat: true }));
    expect(events).toEqual(['down']);
  });

  it('releases on blur / hidden / pagehide', () => {
    detector.keydown(key('ControlRight'));
    vi.advanceTimersByTime(ARM_MS);
    detector.release('blur');
    expect(events).toEqual(['down', 'up:blur']);
  });

  it('Ctrl+Space starts at once, prevents default and is skipped in code editors', () => {
    detector.setChord('hold-ctrl-space');
    expect(detector.keydown(key('Space', { ctrlKey: true }), true)).toBe('ignore');
    expect(events).toEqual([]);
    expect(detector.keydown(key('Space', { ctrlKey: true }))).toBe('prevent');
    expect(detector.keydown(key('Space', { ctrlKey: true, repeat: true }))).toBe('prevent');
    expect(detector.keyup(key('ControlLeft'))).toBe('ignore');
    expect(events).toEqual(['down', 'up:keyup']);
  });

  it('changing the chord mid-hold releases it', () => {
    detector.keydown(key('ControlRight'));
    vi.advanceTimersByTime(ARM_MS);
    detector.setChord('hold-ctrl-space');
    expect(events).toEqual(['down', 'up:cancel']);
  });
});
