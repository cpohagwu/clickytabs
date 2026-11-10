import { describe, expect, it } from 'vitest';

import {
  bytesToBase64,
  colorsMatch,
  dataUrlToBlob,
  fitWithin,
  imageToPage,
  randomSentinel,
  sentinelProbePoints,
  toImageSpace,
} from '@/lib/page-context/capture';
import type { ElementMark } from '@/lib/protocol.gen';

const mark = (bbox: [number, number, number, number]): ElementMark => ({ id: 'e1', role: 'button', bbox });

describe('screenshot sizing', () => {
  it('fits the long edge and the pixel cap, and never upscales', () => {
    expect(fitWithin(2560, 1600, 1024, 1024 * 768)).toMatchObject({ width: 1024, height: 640 });
    expect(fitWithin(1600, 2560, 1024, 1024 * 768)).toMatchObject({ width: 640, height: 1024 });
    const capped = fitWithin(2000, 2000, 1568, 1_150_000);
    expect(capped.width * capped.height).toBeLessThanOrEqual(1_150_000);
    expect(fitWithin(800, 600, 1024, 1024 * 768)).toMatchObject({ width: 800, height: 600, scale: 1 });
  });
});

describe('viewport <-> screenshot mapping', () => {
  // The same CSS layout captured at DPR 1, 1.25 and 2 is resized to the same budget: boxes must land identically.
  for (const dpr of [1, 1.25, 2]) {
    it(`maps CSS boxes into the resized image at DPR ${dpr}`, () => {
      const viewport = { width: 1280, height: 800, dpr, scroll_x: 0, scroll_y: 300 };
      const captured = { width: 1280 * dpr, height: 800 * dpr };
      const image = fitWithin(captured.width, captured.height, 1024, 1024 * 768);
      const [out] = toImageSpace([mark([100, 200, 300, 50])], viewport, image);
      expect(out!.bbox).toEqual([80, 160, 240, 40]);
      const back = imageToPage(80, 160, viewport, image);
      expect(back).toEqual({ x: 100, y: 200 + 300 }); // page coordinates at the capture's scroll position
    });
  }

  it('clips boxes to the viewport and keeps CSS px without a screenshot', () => {
    const viewport = { width: 1000, height: 500 };
    const [partly] = toImageSpace([mark([-50, 450, 200, 100])], viewport, null);
    expect(partly!.bbox).toEqual([0, 450, 150, 50]);
  });
});

describe('encoding', () => {
  it('round-trips bytes through base64 and data URLs', async () => {
    const bytes = Uint8Array.from({ length: 70_000 }, (_, i) => i % 256);
    const b64 = bytesToBase64(bytes);
    const blob = dataUrlToBlob(`data:image/jpeg;base64,${b64}`);
    expect(blob.type).toBe('image/jpeg');
    expect(new Uint8Array(await blob.arrayBuffer())).toEqual(bytes);
  });
});

describe('capture sentinel', () => {
  it('picks mid-range colours and matches them within JPEG tolerance', () => {
    const lowest = randomSentinel(() => 0);
    const highest = randomSentinel(() => 0.9999);
    expect(lowest).toEqual([48, 48, 48]);
    expect(Math.max(...highest)).toBeLessThanOrEqual(207);
    expect(colorsMatch([120, 60, 200], [130, 50, 190])).toBe(true);
    expect(colorsMatch([120, 60, 200], [255, 255, 255])).toBe(false);
  });

  it('probes the sentinel centre and the page just beside it in device pixels', () => {
    expect(sentinelProbePoints(1)).toEqual({ inside: [8, 8], beside: [18, 8] });
    expect(sentinelProbePoints(2)).toEqual({ inside: [16, 16], beside: [36, 16] });
    expect(sentinelProbePoints(1.25)).toEqual({ inside: [10, 10], beside: [23, 10] });
  });
});
