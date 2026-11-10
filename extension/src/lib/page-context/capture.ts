/**
 * Screenshot geometry (pure; used by the service worker).
 *
 * `captureVisibleTab` returns the viewport in device pixels (CSS size × devicePixelRatio, page zoom included). It is
 * scaled down to the active model's budget, and element boxes are mapped from viewport CSS px into the pixels of that
 * final image, which is what the brain sees and what its `x,y` coordinates refer to. Dividing the final size by the
 * CSS viewport size keeps the mapping independent of DPR and zoom.
 */
import type { ElementMark, PageBudget, Viewport } from '../protocol.gen';

export interface Size {
  width: number;
  height: number;
}

/** Largest size within the long-edge and pixel-count limits; never upscales. */
export function fitWithin(width: number, height: number, longEdge: number, maxPixels: number): Size & { scale: number } {
  const scale = Math.min(1, longEdge / Math.max(width, height), Math.sqrt(maxPixels / (width * height)));
  return { width: Math.max(1, Math.floor(width * scale)), height: Math.max(1, Math.floor(height * scale)), scale };
}

const round1 = (value: number): number => Math.round(value * 10) / 10;

/**
 * Viewport CSS boxes -> boxes in `image` pixels, clipped to the visible viewport (the screenshot shows nothing else).
 * Without an image the boxes stay in CSS px (still clipped).
 */
export function toImageSpace(elements: readonly ElementMark[], viewport: Viewport, image: Size | null): ElementMark[] {
  const fx = image ? image.width / viewport.width : 1;
  const fy = image ? image.height / viewport.height : 1;
  return elements.map((element) => {
    const [x, y, w, h] = element.bbox;
    const x0 = Math.min(Math.max(x, 0), viewport.width);
    const y0 = Math.min(Math.max(y, 0), viewport.height);
    const x1 = Math.min(Math.max(x + w, 0), viewport.width);
    const y1 = Math.min(Math.max(y + h, 0), viewport.height);
    return { ...element, bbox: [round1(x0 * fx), round1(y0 * fy), round1((x1 - x0) * fx), round1((y1 - y0) * fy)] };
  });
}

/** Viewport CSS box -> `image` pixels (same mapping as element boxes; used to mask fields). */
export function boxToImage(box: readonly [number, number, number, number], viewport: Viewport, image: Size):
  [number, number, number, number] {
  return toImageSpace([{ id: 'e1', role: 'field', bbox: [box[0], box[1], box[2], box[3]] }], viewport, image)[0]!.bbox;
}

/** Fill for masked text fields in the screenshot: an empty-looking field the brain can still see is there. */
export const FIELD_MASK = { fill: '#e6e6e6', stroke: '#9a9a9a' };

/** Screenshot px -> page CSS px (inverse of the mapping above, at the scroll position of the capture). */
export function imageToPage(x: number, y: number, viewport: Viewport, image: Size | null): { x: number; y: number } {
  const fx = image ? image.width / viewport.width : 1;
  const fy = image ? image.height / viewport.height : 1;
  return { x: x / fx + (viewport.scroll_x ?? 0), y: y / fy + (viewport.scroll_y ?? 0) };
}

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

export function dataUrlToBlob(dataUrl: string): Blob {
  const [head, body = ''] = dataUrl.split(',', 2);
  const mime = /^data:([^;,]+)/.exec(head ?? '')?.[1] ?? 'application/octet-stream';
  const binary = atob(body);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

export type Rgb = [number, number, number];

/**
 * Freshness sentinel: while the companion hides for a capture it shows one square of a random colour at the
 * viewport's top-left corner. Its presence in the capture proves the frame is newer than the hide (captureVisibleTab
 * can return the previous frame). 16 CSS px covers a whole JPEG chroma block at any DPR, so the colour survives.
 */
export const SENTINEL_CSS_PX = 16;

export function randomSentinel(random: () => number = Math.random): Rgb {
  const channel = (): number => 48 + Math.floor(random() * 160); // away from black/white and clipping
  return [channel(), channel(), channel()];
}

export function colorsMatch(a: readonly number[], b: readonly number[], tolerance = 24): boolean {
  return a.length >= 3 && b.length >= 3 && [0, 1, 2].every((i) => Math.abs(a[i]! - b[i]!) <= tolerance);
}

/**
 * Capture-pixel point at the sentinel's centre, and one just right of it (page pixels to paint it over with).
 * `scale` = capture width / CSS viewport width.
 */
export function sentinelProbePoints(scale: number): { inside: [number, number]; beside: [number, number] } {
  const half = Math.round((SENTINEL_CSS_PX / 2) * scale);
  return { inside: [half, half], beside: [Math.ceil((SENTINEL_CSS_PX + 2) * scale), half] };
}

/** Used until the backend's `welcome` announces the active brain's budget. */
export const DEFAULT_PAGE_BUDGET: PageBudget = {
  vision: true,
  image_long_edge: 1024,
  image_max_pixels: 1024 * 768,
  jpeg_quality: 0.8,
  max_elements: 80,
  max_text_chars: 2000,
  max_name_chars: 80,
};

/** The backend's two budgets (brain/budget.py), by name: the brain eval captures its pages at both. */
export const NAMED_PAGE_BUDGETS = {
  compact: DEFAULT_PAGE_BUDGET,
  full: {
    vision: true,
    image_long_edge: 1568,
    image_max_pixels: 1_150_000,
    jpeg_quality: 0.85,
    max_elements: 150,
    max_text_chars: 8000,
    max_name_chars: 100,
  },
} as const satisfies Record<string, PageBudget>;
export type PageBudgetName = keyof typeof NAMED_PAGE_BUDGETS;
