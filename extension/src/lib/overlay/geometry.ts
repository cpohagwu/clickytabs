/** Pure 2-D helpers for the overlay (viewport CSS pixels). */

export interface Point {
  x: number;
  y: number;
}

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Size {
  width: number;
  height: number;
}

export const clamp = (value: number, min: number, max: number): number => Math.min(max, Math.max(min, value));
export const lerp = (a: number, b: number, t: number): number => a + (b - a) * t;
export const distance = (a: Point, b: Point): number => Math.hypot(b.x - a.x, b.y - a.y);
export const center = (r: Rect): Point => ({ x: r.x + r.width / 2, y: r.y + r.height / 2 });

export function rectsOverlap(a: Rect, b: Rect, gap = 0): boolean {
  return a.x < b.x + b.width + gap && b.x < a.x + a.width + gap && a.y < b.y + b.height + gap
    && b.y < a.y + a.height + gap;
}

export function inflate(r: Rect, by: number): Rect {
  return { x: r.x - by, y: r.y - by, width: r.width + 2 * by, height: r.height + 2 * by };
}

export function intersectsViewport(r: Rect, viewport: Size, margin = 0): boolean {
  return r.x + r.width >= -margin && r.y + r.height >= -margin && r.x <= viewport.width + margin
    && r.y <= viewport.height + margin;
}

// ------------------------------------------------------------------------------------------------ easing

export const easeInOutCubic = (t: number): number => (t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2);
export const easeOutCubic = (t: number): number => 1 - (1 - t) ** 3;
export function easeOutBack(t: number): number {
  const c1 = 1.70158;
  return 1 + (c1 + 1) * (t - 1) ** 3 + c1 * (t - 1) ** 2;
}

// ------------------------------------------------------------------------------------------------ curves

export function quadPoint(p0: Point, c: Point, p1: Point, t: number): Point {
  const u = 1 - t;
  return { x: u * u * p0.x + 2 * u * t * c.x + t * t * p1.x, y: u * u * p0.y + 2 * u * t * c.y + t * t * p1.y };
}

/** Tangent direction of the quadratic curve at t (not normalised). */
export function quadTangent(p0: Point, c: Point, p1: Point, t: number): Point {
  return { x: 2 * (1 - t) * (c.x - p0.x) + 2 * t * (p1.x - c.x), y: 2 * (1 - t) * (c.y - p0.y) + 2 * t * (p1.y - c.y) };
}

/**
 * Control point for a graceful arc from `from` to `to`: the chord's midpoint lifted perpendicular to it, bending
 * toward the top of the screen, by a fraction of the distance, kept inside the viewport.
 */
export function arcControl(from: Point, to: Point, viewport: Size, bend = 0.32): Point {
  const mid = { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 };
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const length = Math.hypot(dx, dy) || 1;
  let nx = -dy / length;
  let ny = dx / length;
  if (ny > 0) {
    nx = -nx;
    ny = -ny; // always bulge upward
  }
  const lift = Math.min(260, length * bend);
  return { x: clamp(mid.x + nx * lift, 8, viewport.width - 8), y: clamp(mid.y + ny * lift, 8, viewport.height - 8) };
}

/** Flight time grows with distance but stays snappy: 420 ms .. 1000 ms. */
export function flightDuration(from: Point, to: Point): number {
  return Math.round(clamp(380 + distance(from, to) * 0.55, 420, 1000));
}

/** Where the segment from the rect's center toward `toward` leaves the rect (for arrows between elements). */
export function edgePoint(r: Rect, toward: Point, pad = 6): Point {
  const c = center(r);
  const dx = toward.x - c.x;
  const dy = toward.y - c.y;
  if (dx === 0 && dy === 0) return c;
  const hw = r.width / 2 + pad;
  const hh = r.height / 2 + pad;
  const scale = 1 / Math.max(Math.abs(dx) / (hw || 1), Math.abs(dy) / (hh || 1));
  return { x: c.x + dx * scale, y: c.y + dy * scale };
}

/** The two wing points of an arrowhead whose tip is `tip`, pointing along `direction`. */
export function arrowHead(tip: Point, direction: Point, size = 12, spread = Math.PI / 7): [Point, Point] {
  const angle = Math.atan2(direction.y, direction.x);
  return [
    { x: tip.x - size * Math.cos(angle - spread), y: tip.y - size * Math.sin(angle - spread) },
    { x: tip.x - size * Math.cos(angle + spread), y: tip.y - size * Math.sin(angle + spread) },
  ];
}

/** Top-left corner for a label of `size` next to `anchor`: above it if there is room, else below; kept on screen. */
export function labelPosition(anchor: Rect, size: Size, viewport: Size, gap = 10): Point {
  const x = clamp(anchor.x + anchor.width / 2 - size.width / 2, 8, Math.max(8, viewport.width - size.width - 8));
  const above = anchor.y - gap - size.height;
  const below = anchor.y + anchor.height + gap;
  const y = above >= 8 ? above : below + size.height <= viewport.height - 8 ? below : clamp(above, 8, viewport.height - size.height - 8);
  return { x, y };
}

/** Polyline length (for draw-on animations). */
export function polylineLength(points: readonly Point[]): number {
  let total = 0;
  for (let i = 1; i < points.length; i += 1) total += distance(points[i - 1]!, points[i]!);
  return total;
}

/** Points of the polyline up to `fraction` of its length (the last one interpolated). */
export function partialPolyline(points: readonly Point[], fraction: number): Point[] {
  if (points.length < 2 || fraction >= 1) return [...points];
  const target = polylineLength(points) * clamp(fraction, 0, 1);
  const out: Point[] = [points[0]!];
  let walked = 0;
  for (let i = 1; i < points.length; i += 1) {
    const a = points[i - 1]!;
    const b = points[i]!;
    const step = distance(a, b);
    if (walked + step >= target) {
      const t = step ? (target - walked) / step : 0;
      out.push({ x: lerp(a.x, b.x, t), y: lerp(a.y, b.y, t) });
      return out;
    }
    walked += step;
    out.push(b);
  }
  return out;
}

/** A soft hand-drawn wave under `r` (for the underline mark). */
export function underlineWave(r: Rect, segments = 14, amplitude = 2.2): Point[] {
  const y = r.y + r.height + 5;
  return Array.from({ length: segments + 1 }, (_, i) => ({
    x: r.x + (r.width * i) / segments,
    y: y + Math.sin(i * 1.7) * amplitude,
  }));
}
