/**
 * Scripted overlay tour for whatever page is open (demo mode, no backend needed).
 *
 * It picks real elements (main heading, something clickable, a paragraph, an image), registers them like the DOM
 * set-of-marks would, and returns spoken-style steps whose directives carry `at_char` offsets, so playback runs
 * through the same scheduler and directive code as a real brain turn.
 */
import { APP_NAME } from '../brand';
import type { Directive } from './directives';
import { center, distance, intersectsViewport, type Size } from './geometry';
import type { ElementRegistry } from './targets';

export interface DemoStep {
  text: string;
  durationMs: number;
  directives: { at_char: number; directive: Directive }[];
}

const MS_PER_CHAR = 48;
const MIN_STEP_MS = 1_600;
/** An arrow between closer elements would hide under their labels. */
const MIN_ARROW_PX = 120;

function isVisible(element: Element, viewport: Size, minSize = 6): boolean {
  const rect = element.getBoundingClientRect();
  if (rect.width < minSize || rect.height < minSize) return false;
  if (!intersectsViewport(rect, viewport, -4)) return false;
  // Catches what a rect cannot: content of closed <details>, content-visibility, hidden/transparent ancestors.
  if (typeof element.checkVisibility === 'function') {
    return element.checkVisibility({ contentVisibilityAuto: true, opacityProperty: true, visibilityProperty: true });
  }
  const style = getComputedStyle(element);
  return style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity || '1') > 0.1;
}

function pick(doc: Document, selector: string, viewport: Size, accept: (el: Element) => boolean = () => true): Element | null {
  for (const element of doc.querySelectorAll(selector)) {
    if (element.closest('clickytabs-ui')) continue;
    if (isVisible(element, viewport) && accept(element)) return element;
  }
  return null;
}

/** Rendered text where available (innerText skips hidden tooltips and the like), else the raw text content. */
const textOf = (el: Element): string =>
  ((el instanceof HTMLElement ? el.innerText : null) ?? el.textContent ?? '').replace(/\s+/g, ' ').trim();

/** Prose, not a row of links (nav bars and link lists make poor underline targets). */
function isProse(el: Element): boolean {
  const text = textOf(el);
  const linked = [...el.querySelectorAll('a')].reduce((sum, a) => sum + textOf(a).length, 0);
  return text.length >= 40 && linked / text.length < 0.5;
}

function step(text: string, directives: { phrase: string; directive: Directive }[] = []): DemoStep {
  return {
    text,
    durationMs: Math.max(MIN_STEP_MS, text.length * MS_PER_CHAR),
    directives: directives.map(({ phrase, directive }) => ({
      at_char: Math.max(0, phrase === '$end' ? text.length : text.indexOf(phrase)),
      directive,
    })),
  };
}

/** A freehand loop around a point (viewport px). */
function scribble(cx: number, cy: number, r: number): [number, number][] {
  return Array.from({ length: 28 }, (_, i) => {
    const a = (i / 24) * Math.PI * 2 - Math.PI / 2;
    const wobble = 1 + Math.sin(i * 2.3) * 0.06;
    return [Math.round(cx + Math.cos(a) * r * 1.35 * wobble), Math.round(cy + Math.sin(a) * r * wobble)];
  });
}

/** The two picked elements farthest apart (ids), if at least MIN_ARROW_PX apart. */
function farthestPair(picks: [string, Element | null][]): [string, string] | null {
  const present = picks.filter((p): p is [string, Element] => p[1] !== null);
  let best: [string, string] | null = null;
  let bestDistance = MIN_ARROW_PX;
  for (let i = 0; i < present.length; i += 1) {
    for (let j = i + 1; j < present.length; j += 1) {
      const d = distance(center(present[i]![1].getBoundingClientRect()), center(present[j]![1].getBoundingClientRect()));
      if (d >= bestDistance) {
        bestDistance = d;
        best = [present[i]![0], present[j]![0]];
      }
    }
  }
  return best;
}

export function planDemo(doc: Document, registry: ElementRegistry, viewport: Size): DemoStep[] {
  const heading = pick(doc, 'h1, h2, [role="heading"]', viewport, (el) => textOf(el).length > 0);
  const action = pick(
    doc,
    'button, a[href], [role="button"], input[type="submit"], input[type="button"]',
    viewport,
    (el) => el !== heading && !el.contains(heading) && (textOf(el).length > 0 || el.getAttribute('aria-label') !== null),
  );
  const paragraph = pick(doc, 'p, li, blockquote, dd', viewport, (el) => isProse(el) && !(action && el.contains(action)));
  const media = pick(doc, 'img, video, canvas, svg, picture', viewport, (el) => {
    const r = el.getBoundingClientRect();
    return r.width >= 48 && r.height >= 48;
  });

  const steps: DemoStep[] = [step(`Hi, I'm ${APP_NAME}. Here's what I can do on this page.`)];
  const ids = {
    heading: heading ? registry.register(heading) : '',
    action: action ? registry.register(action) : '',
    paragraph: paragraph ? registry.register(paragraph) : '',
    media: media ? registry.register(media) : '',
  };
  if (heading) {
    steps.push(step('This is the main heading.', [
      { phrase: 'main heading', directive: { kind: 'point', to: ids.heading, label: 'Main heading' } },
      { phrase: '$end', directive: { kind: 'mark', to: ids.heading, style: 'circle' } },
    ]));
  }
  if (action) {
    steps.push(step('And here is something you can click.', [
      { phrase: 'something', directive: { kind: 'point', to: ids.action, label: 'Clickable' } },
      { phrase: 'click', directive: { kind: 'mark', to: ids.action, style: 'box' } },
    ]));
  }
  if (paragraph) {
    steps.push(step('I can underline the part that matters.', [
      { phrase: 'underline', directive: { kind: 'mark', to: ids.paragraph, style: 'underline' } },
    ]));
  }
  const ends = farthestPair([
    [ids.heading, heading],
    [ids.action, action],
    [ids.paragraph, paragraph],
    [ids.media, media],
  ]);
  if (ends) {
    steps.push(step('And connect related things with an arrow.', [
      { phrase: 'arrow', directive: { kind: 'arrow', source: ends[0], target: ends[1] } },
    ]));
  }
  if (media) {
    steps.push(step('Pictures get a soft glow.', [
      { phrase: 'Pictures', directive: { kind: 'mark', to: ids.media, style: 'glow', label: 'Image' } },
    ]));
  }
  steps.push(step('I can even sketch freehand.', [
    {
      phrase: 'sketch',
      directive: { kind: 'path', points: scribble(viewport.width / 2, viewport.height / 2, Math.min(90, viewport.height / 6)), label: 'Like this' },
    },
  ]));
  steps.push(step('Hold Right Ctrl to talk to me, or type in my panel. Cleaning up now.', [
    { phrase: 'Cleaning', directive: { kind: 'clear' } },
  ]));
  return steps;
}
