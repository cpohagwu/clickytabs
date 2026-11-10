/**
 * Applies the brain's visual directives (protocol `Directive`) to the overlay. The demo uses the same entry point, so
 * it exercises exactly the path the brain drives.
 *
 * Element references are registry ids ("e17", from the turn's DOM snapshot); "x,y" strings and point directives use
 * screenshot pixels, converted by `toPage` with the geometry of the turn's capture.
 */
import type { TimedDirective } from '../protocol.gen';
import { intersectsViewport, type Point } from './geometry';
import type { OverlayRenderer } from './renderer';
import { type ElementRegistry, elementTarget, type Target, targetRect } from './targets';

export type Directive = TimedDirective['directive'];

export interface DirectiveHost {
  renderer: OverlayRenderer;
  registry: ElementRegistry;
  /** Where pointer flights start: the orb's center, in viewport px. */
  origin(): Point;
  /** Screenshot px -> page CSS px, at the scroll position of the capture. Default: viewport px at the current scroll. */
  toPage?(x: number, y: number): Point;
  /** Smooth-scroll wait before pointing at an element that is off screen. */
  scrollSettleMs?: number;
}

const POINT_REF = /^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/;

/** "e17" -> that element; "120,340" -> a page point; anything unknown -> null (the directive is skipped). */
export function resolveRef(ref: string | null | undefined, host: DirectiveHost): Target | null {
  if (!ref) return null;
  const element = host.registry.get(ref.trim());
  if (element) return elementTarget(element);
  const match = POINT_REF.exec(ref);
  if (!match) return null;
  const page = toPage(host, Number(match[1]), Number(match[2]));
  return { kind: 'page-point', x: page.x, y: page.y };
}

function toPage(host: DirectiveHost, x: number, y: number): Point {
  return host.toPage ? host.toPage(x, y) : { x: x + scrollX, y: y + scrollY };
}

async function ensureVisible(target: Target, host: DirectiveHost): Promise<void> {
  if (target.kind !== 'element') return;
  const rect = targetRect(target);
  if (rect && intersectsViewport(rect, { width: innerWidth, height: innerHeight }, -24)) return;
  target.element.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
  await new Promise((resolve) => setTimeout(resolve, host.scrollSettleMs ?? 450));
}

/** Resolves when the directive's animation has started (pointer flights: when the pointer lands). */
export async function applyDirective(directive: Directive, host: DirectiveHost): Promise<void> {
  const { renderer } = host;
  switch (directive.kind) {
    case 'point': {
      const target = directive.to
        ? resolveRef(directive.to, host)
        : directive.x != null && directive.y != null
          ? resolveRef(`${directive.x},${directive.y}`, host)
          : null;
      if (!target) return;
      await ensureVisible(target, host);
      await renderer.pointTo(target, host.origin(), directive.label);
      return;
    }
    case 'mark': {
      const target = resolveRef(directive.to, host);
      if (!target) return;
      await ensureVisible(target, host);
      renderer.mark(target, directive.style ?? 'circle', directive.label);
      return;
    }
    case 'arrow': {
      const from = resolveRef(directive.source, host);
      const to = resolveRef(directive.target, host);
      if (from && to) renderer.arrow(from, to, directive.label);
      return;
    }
    case 'path': {
      // The renderer pins viewport points to the page at the current scroll.
      const points = directive.points.map(([x, y]) => toPage(host, x, y)).map((p) => ({ x: p.x - scrollX, y: p.y - scrollY }));
      renderer.path(points, directive.label);
      return;
    }
    case 'scroll': {
      const target = resolveRef(directive.to, host);
      if (target) await ensureVisible(target, host);
      return;
    }
    case 'clear':
      renderer.clear();
      return;
  }
}
