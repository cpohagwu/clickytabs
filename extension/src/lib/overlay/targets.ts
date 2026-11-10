/**
 * What an annotation points at, resolved to a *live* viewport rect every frame (scroll- and layout-proof).
 *
 * Elements are referenced by registry ids ("e1".."eN"), exactly like the brain's directives (`<point to="e17"/>`).
 * The DOM set-of-marks snapshot fills the registry; the overlay demo registers the elements it picks.
 * Fixed points are stored in page coordinates so they stay attached to the content while scrolling.
 */
import type { Point, Rect } from './geometry';

export type Target = { kind: 'element'; element: Element } | { kind: 'page-point'; x: number; y: number };

export function elementTarget(element: Element): Target {
  return { kind: 'element', element };
}

/** A viewport point (CSS px) pinned to the page at the current scroll position. */
export function pagePointTarget(viewportPoint: Point, scroll: Point = { x: scrollX, y: scrollY }): Target {
  return { kind: 'page-point', x: viewportPoint.x + scroll.x, y: viewportPoint.y + scroll.y };
}

/** Controls and media are outlined by their own box; text containers by the box of their text. */
const OWN_BOX = 'a, button, input, select, textarea, img, video, canvas, svg, picture, iframe, [role="button"], [role="link"]';

/**
 * Tight box around the element's content. A block-level heading or paragraph spans its whole container, so its own
 * box would put the pointer in empty space and stretch a circle across the page.
 */
function contentRect(element: Element, box: DOMRect): DOMRect | null {
  if (element.matches(OWN_BOX)) return null;
  const range = element.ownerDocument.createRange();
  range.selectNodeContents(element);
  const r = range.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && r.width < box.width - 8 ? r : null;
}

/** Current viewport rect of the target, or null when it is gone (detached, display:none, zero-sized). */
export function targetRect(target: Target, scroll: Point = { x: scrollX, y: scrollY }): Rect | null {
  if (target.kind === 'page-point') return { x: target.x - scroll.x, y: target.y - scroll.y, width: 0, height: 0 };
  if (!target.element.isConnected) return null;
  const box = target.element.getBoundingClientRect();
  if (box.width === 0 && box.height === 0) return null;
  const r = contentRect(target.element, box) ?? box;
  return { x: r.x, y: r.y, width: r.width, height: r.height };
}

export class ElementRegistry {
  private readonly byId = new Map<string, WeakRef<Element>>();
  private readonly ids = new WeakMap<Element, string>();
  private next = 1;

  /** Returns the element's id, assigning the next "eN" the first time it is seen. */
  register(element: Element): string {
    const known = this.ids.get(element);
    if (known && this.byId.get(known)?.deref() === element) return known;
    const id = `e${this.next}`;
    this.next += 1;
    this.byId.set(id, new WeakRef(element));
    this.ids.set(element, id);
    return id;
  }

  get(id: string): Element | null {
    const element = this.byId.get(id)?.deref() ?? null;
    return element?.isConnected ? element : null;
  }

  get size(): number {
    return this.byId.size;
  }

  /** A new snapshot replaces the previous ids. */
  reset(): void {
    this.byId.clear();
    this.next = 1;
  }
}
