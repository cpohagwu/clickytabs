/**
 * The pointer overlay: one full-viewport, pointer-events:none canvas in the companion's shadow root.
 *
 * - A rAF loop runs only while something is on screen; every frame re-reads target rects, so annotations stay on
 *   their elements through scrolling, resizing and layout changes.
 * - Pointer flights: a comet travels from the orb along an upward arc and lands with a pulse + label.
 * - Marks draw themselves on (circle, box, underline, glow), arrows and freehand paths too; everything fades out
 *   after `ttlMs` unless cleared earlier.
 * - prefers-reduced-motion: no flights, no breathing; shapes appear without draw-on.
 */
import {
  arcControl,
  arrowHead,
  center,
  clamp,
  easeInOutCubic,
  easeOutCubic,
  edgePoint,
  flightDuration,
  inflate,
  intersectsViewport,
  labelPosition,
  partialPolyline,
  type Point,
  quadPoint,
  type Rect,
  rectsOverlap,
  type Size,
  underlineWave,
} from './geometry';
import { type Target, targetRect } from './targets';

export type MarkStyle = 'circle' | 'box' | 'underline' | 'glow';

interface ShapeBase {
  id: number;
  born: number;
  ttl: number;
  label?: string | null;
}

type Shape =
  | (ShapeBase & { kind: 'mark'; style: MarkStyle; target: Target })
  | (ShapeBase & { kind: 'arrow'; from: Target; to: Target })
  | (ShapeBase & { kind: 'path'; points: Point[] }) // page coordinates
  | (ShapeBase & { kind: 'spot'; target: Target });

/** A shape before the renderer stamps it (Omit distributed over the union). */
type NewShape = Shape extends infer S ? (S extends Shape ? Omit<S, 'id' | 'born' | 'ttl'> : never) : never;

interface Flight {
  from: Point;
  target: Target;
  born: number;
  duration: number;
  label?: string | null;
  trail: Point[];
  resolve: () => void;
}

export interface RendererOptions {
  reducedMotion?: boolean;
  ttlMs?: number;
  now?: () => number;
}

export const DEFAULT_TTL_MS = 14_000;
/** Landing spots are a "here" gesture: they fade well before the marks they usually come with. */
const SPOT_TTL_MS = 4_500;
const LABEL_HEIGHT = 26;
const FADE_MS = 650;
const DRAW_ON_MS = 520;
const TRAIL = 16;
const CYAN = '#00f0ff';
const VIOLET = '#7c5cff';
const MAGENTA = '#ff2bd6';
const FONT = '600 12.5px Inter, "Segoe UI", system-ui, -apple-system, sans-serif';

export class OverlayRenderer {
  private readonly ctx: CanvasRenderingContext2D;
  private shapes: Shape[] = [];
  private flights: Flight[] = [];
  private frame: number | null = null;
  private nextId = 1;
  private dpr = 1;
  private viewport: Size = { width: 0, height: 0 };
  private readonly onResize = (): void => {
    this.resize();
    this.wake();
  };

  constructor(
    private readonly canvas: HTMLCanvasElement,
    private readonly options: RendererOptions = {},
  ) {
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('2D canvas unavailable');
    this.ctx = ctx;
    this.resize();
    window.addEventListener('resize', this.onResize, { passive: true });
  }

  /** Shapes currently on screen (flights excluded). */
  get count(): number {
    return this.shapes.length;
  }

  get animating(): boolean {
    return this.frame !== null || this.flights.length > 0;
  }

  set reducedMotion(value: boolean) {
    this.options.reducedMotion = value;
  }

  // ------------------------------------------------------------------------------------------------ API

  /** Fly the pointer from `from` (viewport px) to the target; resolves when it lands. */
  pointTo(target: Target, from: Point, label?: string | null): Promise<void> {
    const rect = targetRect(target);
    if (!rect) return Promise.resolve();
    if (this.options.reducedMotion) {
      this.add({ kind: 'spot', target, label }, SPOT_TTL_MS);
      return Promise.resolve();
    }
    return new Promise<void>((resolve) => {
      this.flights.push({
        from,
        target,
        born: this.now(),
        duration: flightDuration(from, center(rect)),
        label,
        trail: [],
        resolve,
      });
      this.wake();
    });
  }

  mark(target: Target, style: MarkStyle = 'circle', label?: string | null): number {
    return this.add({ kind: 'mark', style, target, label });
  }

  arrow(from: Target, to: Target, label?: string | null): number {
    return this.add({ kind: 'arrow', from, to, label });
  }

  /** Freehand stroke through viewport points (pinned to the page at the current scroll position). */
  path(viewportPoints: readonly Point[], label?: string | null): number {
    const points = viewportPoints.map((p) => ({ x: p.x + scrollX, y: p.y + scrollY }));
    return this.add({ kind: 'path', points, label });
  }

  /** Fade everything out now; pending flights land immediately. */
  clear(): void {
    const now = this.now();
    for (const flight of this.flights) flight.resolve();
    this.flights = [];
    for (const shape of this.shapes) shape.ttl = Math.min(shape.ttl, now - shape.born + FADE_MS);
    this.wake();
  }

  destroy(): void {
    window.removeEventListener('resize', this.onResize);
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    this.frame = null;
    for (const flight of this.flights) flight.resolve();
    this.flights = [];
    this.shapes = [];
  }

  // ------------------------------------------------------------------------------------------------ loop

  private now(): number {
    return this.options.now ? this.options.now() : performance.now();
  }

  private add(shape: NewShape, ttl = this.options.ttlMs ?? DEFAULT_TTL_MS): number {
    const id = this.nextId;
    this.nextId += 1;
    this.shapes.push({ ...shape, id, born: this.now(), ttl } as Shape);
    this.wake();
    return id;
  }

  private resize(): void {
    this.dpr = Math.max(1, window.devicePixelRatio || 1);
    this.viewport = { width: window.innerWidth, height: window.innerHeight };
    this.canvas.width = Math.round(this.viewport.width * this.dpr);
    this.canvas.height = Math.round(this.viewport.height * this.dpr);
  }

  private wake(): void {
    if (this.frame === null) this.frame = requestAnimationFrame(() => this.tick());
  }

  private tick(): void {
    this.frame = null;
    const now = this.now();
    const { ctx } = this;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.viewport.width, this.viewport.height);
    this.shapes = this.shapes.filter((shape) => now - shape.born < shape.ttl);
    const labels: { anchor: Rect; text: string; alpha: number }[] = [];
    for (const shape of this.shapes) {
      const label = this.drawShape(shape, now);
      if (label) labels.push(label);
    }
    for (const flight of [...this.flights]) {
      if (this.drawFlight(flight, now)) {
        this.flights = this.flights.filter((f) => f !== flight);
        this.add({ kind: 'spot', target: flight.target, label: flight.label }, SPOT_TTL_MS);
        flight.resolve();
      }
    }
    const placed: Rect[] = [];
    for (const label of labels) this.drawLabel(label.anchor, label.text, label.alpha, placed);
    if (this.shapes.length > 0 || this.flights.length > 0) this.wake();
    else ctx.clearRect(0, 0, this.viewport.width, this.viewport.height);
  }

  // ------------------------------------------------------------------------------------------------ shapes

  /** Draws one shape; returns its label (drawn last, on top) if it has one. */
  private drawShape(shape: Shape, now: number): { anchor: Rect; text: string; alpha: number } | null {
    const age = now - shape.born;
    const fade = clamp((shape.ttl - age) / FADE_MS, 0, 1);
    const progress = this.options.reducedMotion ? 1 : easeOutCubic(clamp(age / DRAW_ON_MS, 0, 1));
    const breathe = this.options.reducedMotion ? 0 : Math.sin(now / 320) * 0.5 + 0.5;
    let anchor: Rect | null = null;

    switch (shape.kind) {
      case 'mark': {
        const rect = targetRect(shape.target);
        if (!rect || !intersectsViewport(rect, this.viewport, 40)) return null;
        anchor = rect;
        this.drawMark(shape.style, rect, progress, breathe, fade);
        break;
      }
      case 'spot': {
        const rect = targetRect(shape.target);
        if (!rect || !intersectsViewport(rect, this.viewport, 40)) return null;
        anchor = rect;
        this.drawSpot(rect, age, fade);
        break;
      }
      case 'arrow': {
        const from = targetRect(shape.from);
        const to = targetRect(shape.to);
        if (!from || !to) return null;
        anchor = to;
        this.drawArrow(from, to, progress, fade);
        break;
      }
      case 'path': {
        const points = shape.points.map((p) => ({ x: p.x - scrollX, y: p.y - scrollY }));
        if (points.length < 2) return null;
        anchor = boundsOf(points);
        this.stroke(partialPolyline(points, progress), fade, 3.5);
        break;
      }
    }
    if (!shape.label || !anchor) return null;
    return { anchor, text: shape.label, alpha: fade * clamp(age / 260, 0, 1) };
  }

  private gradient(from: Point, to: Point): CanvasGradient {
    const g = this.ctx.createLinearGradient(from.x, from.y, to.x, to.y);
    g.addColorStop(0, CYAN);
    g.addColorStop(0.5, VIOLET);
    g.addColorStop(1, MAGENTA);
    return g;
  }

  private stroke(points: readonly Point[], alpha: number, width: number): void {
    if (points.length < 2) return;
    const { ctx } = this;
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.lineWidth = width;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.shadowColor = CYAN;
    ctx.shadowBlur = 14;
    ctx.strokeStyle = this.gradient(points[0]!, points[points.length - 1]!);
    ctx.beginPath();
    ctx.moveTo(points[0]!.x, points[0]!.y);
    for (let i = 1; i < points.length - 1; i += 1) {
      const mid = { x: (points[i]!.x + points[i + 1]!.x) / 2, y: (points[i]!.y + points[i + 1]!.y) / 2 };
      ctx.quadraticCurveTo(points[i]!.x, points[i]!.y, mid.x, mid.y);
    }
    const last = points[points.length - 1]!;
    ctx.lineTo(last.x, last.y);
    ctx.stroke();
    ctx.restore();
  }

  private drawMark(style: MarkStyle, rect: Rect, progress: number, breathe: number, alpha: number): void {
    const { ctx } = this;
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.lineCap = 'round';
    ctx.shadowColor = CYAN;
    ctx.shadowBlur = 16 + breathe * 8;
    if (style === 'circle') {
      const r = inflate(rect, 10);
      const c = center(r);
      ctx.strokeStyle = this.gradient({ x: r.x, y: r.y }, { x: r.x + r.width, y: r.y + r.height });
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.ellipse(c.x, c.y, r.width / 2 + breathe * 2, r.height / 2 + breathe * 2, 0, -Math.PI / 2,
        -Math.PI / 2 + Math.PI * 2 * progress);
      ctx.stroke();
    } else if (style === 'box') {
      const r = inflate(rect, 6);
      const perimeter = 2 * (r.width + r.height);
      ctx.strokeStyle = this.gradient({ x: r.x, y: r.y }, { x: r.x + r.width, y: r.y + r.height });
      ctx.lineWidth = 2.5;
      ctx.setLineDash([perimeter * progress, perimeter]);
      ctx.beginPath();
      ctx.roundRect(r.x, r.y, r.width, r.height, 10);
      ctx.stroke();
    } else if (style === 'underline') {
      ctx.restore();
      this.stroke(partialPolyline(underlineWave(rect), progress), alpha, 3);
      return;
    } else {
      const r = inflate(rect, 6);
      ctx.fillStyle = `rgba(0, 240, 255, ${(0.08 + breathe * 0.07) * progress})`;
      ctx.strokeStyle = `rgba(124, 92, 255, ${0.55 * progress})`;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.roundRect(r.x, r.y, r.width, r.height, 12);
      ctx.fill();
      ctx.stroke();
    }
    ctx.restore();
  }

  private drawSpot(rect: Rect, age: number, alpha: number): void {
    const { ctx } = this;
    const c = center(rect);
    ctx.save();
    ctx.globalAlpha = alpha;
    // Two expanding rings right after landing, then a steady glowing dot.
    for (const delay of [0, 220]) {
      const t = clamp((age - delay) / 900, 0, 1);
      if (t <= 0 || t >= 1) continue;
      ctx.strokeStyle = `rgba(255, 43, 214, ${0.75 * (1 - t)})`;
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      ctx.arc(c.x, c.y, 8 + 34 * easeOutCubic(t), 0, Math.PI * 2);
      ctx.stroke();
    }
    const glow = ctx.createRadialGradient(c.x, c.y, 0, c.x, c.y, 12);
    glow.addColorStop(0, 'rgba(255, 255, 255, 0.95)');
    glow.addColorStop(0.45, 'rgba(0, 240, 255, 0.9)');
    glow.addColorStop(1, 'rgba(124, 92, 255, 0)');
    ctx.fillStyle = glow;
    ctx.shadowColor = CYAN;
    ctx.shadowBlur = 18;
    ctx.beginPath();
    ctx.arc(c.x, c.y, 12, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  private drawArrow(fromRect: Rect, toRect: Rect, progress: number, alpha: number): void {
    const start = edgePoint(fromRect, center(toRect));
    const end = edgePoint(toRect, center(fromRect), 10);
    const control = arcControl(start, end, this.viewport, 0.25);
    const samples = Array.from({ length: 33 }, (_, i) => quadPoint(start, control, end, i / 32));
    const visible = partialPolyline(samples, progress);
    this.stroke(visible, alpha, 3);
    if (progress < 0.97 || visible.length < 2) return;
    const tip = visible[visible.length - 1]!;
    const before = visible[visible.length - 2]!;
    const [left, right] = arrowHead(tip, { x: tip.x - before.x, y: tip.y - before.y }, 14);
    this.stroke([left, tip, right], alpha, 3);
  }

  private drawFlight(flight: Flight, now: number): boolean {
    const rect = targetRect(flight.target);
    if (!rect) return true;
    const t = clamp((now - flight.born) / flight.duration, 0, 1);
    const to = center(rect);
    const control = arcControl(flight.from, to, this.viewport);
    const head = quadPoint(flight.from, control, to, easeInOutCubic(t));
    flight.trail.push(head);
    if (flight.trail.length > TRAIL) flight.trail.shift();

    const { ctx } = this;
    ctx.save();
    ctx.lineCap = 'round';
    for (let i = 1; i < flight.trail.length; i += 1) {
      const k = i / flight.trail.length;
      ctx.globalAlpha = k * 0.8;
      ctx.strokeStyle = i % 2 ? MAGENTA : CYAN;
      ctx.lineWidth = 1 + k * 5;
      ctx.beginPath();
      ctx.moveTo(flight.trail[i - 1]!.x, flight.trail[i - 1]!.y);
      ctx.lineTo(flight.trail[i]!.x, flight.trail[i]!.y);
      ctx.stroke();
    }
    const glow = ctx.createRadialGradient(head.x, head.y, 0, head.x, head.y, 10);
    glow.addColorStop(0, '#ffffff');
    glow.addColorStop(0.5, CYAN);
    glow.addColorStop(1, 'rgba(124, 92, 255, 0)');
    ctx.globalAlpha = 1;
    ctx.fillStyle = glow;
    ctx.shadowColor = CYAN;
    ctx.shadowBlur = 24;
    ctx.beginPath();
    ctx.arc(head.x, head.y, 10, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
    return t >= 1;
  }

  /** Draws a label pill beside its anchor, stepping clear of the pills already `placed` this frame. */
  private drawLabel(anchor: Rect, text: string, alpha: number, placed: Rect[]): void {
    if (alpha <= 0.01) return;
    const { ctx } = this;
    ctx.save();
    ctx.font = FONT;
    const label = text.length > 60 ? `${text.slice(0, 57)}…` : text;
    const width = ctx.measureText(label).width + 22;
    const height = LABEL_HEIGHT;
    const at = labelPosition(inflate(anchor, 8), { width, height }, this.viewport);
    const step = at.y + height / 2 < anchor.y + anchor.height / 2 ? -(height + 4) : height + 4; // away from the anchor
    for (let tries = 0; tries < 6 && placed.some((r) => rectsOverlap(r, { ...at, width, height }, 2)); tries += 1) {
      at.y = clamp(at.y + step, 4, this.viewport.height - height - 4);
    }
    placed.push({ ...at, width, height });
    ctx.globalAlpha = alpha;
    ctx.shadowColor = 'rgba(0, 0, 0, 0.5)';
    ctx.shadowBlur = 16;
    ctx.fillStyle = 'rgba(10, 12, 24, 0.88)';
    ctx.beginPath();
    ctx.roundRect(at.x, at.y, width, height, height / 2);
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.lineWidth = 1.2;
    ctx.strokeStyle = this.gradient({ x: at.x, y: at.y }, { x: at.x + width, y: at.y + height });
    ctx.stroke();
    ctx.fillStyle = '#eef1ff';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, at.x + 11, at.y + height / 2 + 0.5);
    ctx.restore();
  }
}

function boundsOf(points: readonly Point[]): Rect {
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const x = Math.min(...xs);
  const y = Math.min(...ys);
  return { x, y, width: Math.max(...xs) - x, height: Math.max(...ys) - y };
}
