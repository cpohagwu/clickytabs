import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  arcControl,
  edgePoint,
  flightDuration,
  intersectsViewport,
  labelPosition,
  partialPolyline,
  polylineLength,
  quadPoint,
} from '@/lib/overlay/geometry';
import { offsetForChar, scheduleSegment, Timeline } from '@/lib/overlay/scheduler';

const VIEWPORT = { width: 1280, height: 800 };

describe('overlay geometry', () => {
  it('arcs pointer flights upward in both directions and keeps the control point on screen', () => {
    expect(arcControl({ x: 100, y: 500 }, { x: 600, y: 500 }, VIEWPORT).y).toBeLessThan(500);
    expect(arcControl({ x: 600, y: 500 }, { x: 100, y: 500 }, VIEWPORT).y).toBeLessThan(500);
    const nearTop = arcControl({ x: 0, y: 10 }, { x: 1200, y: 10 }, VIEWPORT);
    expect(nearTop.y).toBeGreaterThanOrEqual(8);
    expect(nearTop.x).toBeLessThanOrEqual(VIEWPORT.width - 8);
  });

  it('evaluates the bezier at its endpoints', () => {
    const a = { x: 10, y: 20 };
    const c = { x: 300, y: -40 };
    const b = { x: 600, y: 200 };
    expect(quadPoint(a, c, b, 0)).toEqual(a);
    expect(quadPoint(a, c, b, 1)).toEqual(b);
  });

  it('times flights between 420 ms and 1 s', () => {
    expect(flightDuration({ x: 0, y: 0 }, { x: 1, y: 1 })).toBe(420);
    expect(flightDuration({ x: 0, y: 0 }, { x: 5000, y: 5000 })).toBe(1000);
  });

  it('finds the padded edge of a rect toward a point', () => {
    const rect = { x: 100, y: 100, width: 200, height: 50 };
    expect(edgePoint(rect, { x: 1000, y: 125 }, 6)).toEqual({ x: 306, y: 125 });
    expect(edgePoint(rect, { x: 200, y: -500 }, 0)).toEqual({ x: 200, y: 100 });
  });

  it('puts labels above the anchor when there is room, else below, always inside the viewport', () => {
    const size = { width: 120, height: 26 };
    const above = labelPosition({ x: 600, y: 300, width: 80, height: 40 }, size, VIEWPORT);
    expect(above.y).toBeLessThan(300);
    const below = labelPosition({ x: 600, y: 4, width: 80, height: 40 }, size, VIEWPORT);
    expect(below.y).toBeGreaterThan(44);
    const clipped = labelPosition({ x: 1270, y: 300, width: 40, height: 40 }, size, VIEWPORT);
    expect(clipped.x + size.width).toBeLessThanOrEqual(VIEWPORT.width - 8);
  });

  it('draws polylines on progressively', () => {
    const line = [{ x: 0, y: 0 }, { x: 100, y: 0 }, { x: 100, y: 100 }];
    expect(polylineLength(line)).toBe(200);
    expect(polylineLength(partialPolyline(line, 0.5))).toBeCloseTo(100, 5);
    expect(partialPolyline(line, 1)).toEqual(line);
  });

  it('knows when a rect is on screen', () => {
    expect(intersectsViewport({ x: 10, y: 10, width: 5, height: 5 }, VIEWPORT)).toBe(true);
    expect(intersectsViewport({ x: 10, y: 2000, width: 5, height: 5 }, VIEWPORT)).toBe(false);
    expect(intersectsViewport({ x: -30, y: 10, width: 5, height: 5 }, VIEWPORT, 40)).toBe(true);
  });
});

describe('directive scheduler', () => {
  it('maps a character offset to a time in the spoken segment', () => {
    expect(offsetForChar(5, 10, 1000)).toBe(500);
    expect(offsetForChar(50, 10, 1000)).toBe(1000);
    expect(offsetForChar(-3, 10, 1000)).toBe(0);
    expect(offsetForChar(3, 0, 1000)).toBe(0);
  });

  it('orders items by firing time, keeps ties stable and prefers TTS alignment', () => {
    const text = 'Click this button, then that one.';
    const items = [
      { at_char: 24, item: 'that' },
      { at_char: 6, item: 'this' },
      { at_char: 6, item: 'this-too' },
    ];
    expect(scheduleSegment(text, 3300, items).map((s) => s.item)).toEqual(['this', 'this-too', 'that']);
    const aligned = scheduleSegment(text, 3300, items, Array.from({ length: text.length }, (_, i) => i * 10));
    expect(aligned[0]).toEqual({ delayMs: 60, item: 'this' });
    expect(aligned[2]).toEqual({ delayMs: 240, item: 'that' });
  });
});

describe('Timeline', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('fires callbacks in time and cancels everything still pending', async () => {
    vi.useFakeTimers();
    const timeline = new Timeline();
    const fired: string[] = [];
    timeline.at(100, () => fired.push('a'));
    timeline.at(300, () => fired.push('b'));
    expect(timeline.pending).toBe(2);
    await vi.advanceTimersByTimeAsync(150);
    expect(fired).toEqual(['a']);
    timeline.cancel();
    await vi.advanceTimersByTimeAsync(500);
    expect(fired).toEqual(['a']);
    expect(timeline.pending).toBe(0);
    timeline.at(10, () => fired.push('late'));
    await vi.advanceTimersByTimeAsync(50);
    expect(fired).toEqual(['a']);
  });

  it('sleeps until the delay passes', async () => {
    vi.useFakeTimers();
    const timeline = new Timeline();
    let woke = false;
    void timeline.sleep(200).then(() => (woke = true));
    await vi.advanceTimersByTimeAsync(199);
    expect(woke).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(woke).toBe(true);
  });
});
