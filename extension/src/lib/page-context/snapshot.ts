/**
 * DOM set-of-marks for a turn (content script, top frame).
 *
 * The visible interactive elements, headings, media and landmarks, each with a role, an accessible name, a few
 * states and a viewport box; plus budgeted visible text and the selection. Marks are registered as "e1".."eN" so the
 * brain's directives (`<point to="e17"/>`) resolve to live elements.
 *
 * Privacy: names come from ARIA, labels, alt text, placeholders and page text, never from what the user typed. Input,
 * select and textarea values and editable content are never read; password fields only ever contribute their label.
 * The boxes of all visible text fields are reported separately (`field_boxes`), so the worker can mask what they show
 * in the screenshot. Secret-looking URL parameters are redacted.
 *
 * Speed (target < 30 ms): candidates come from native querySelectorAll per priority group on the document and every
 * open shadow root (no per-element selector matching in JS); the costly checks (checkVisibility, hit-testing) run in
 * priority order only until the budget is full; the visible-text walk skips subtrees known to be hidden and is
 * time-boxed.
 */
import type { ElementMark, Viewport } from '../protocol.gen';
import type { ElementRegistry } from '../overlay/targets';

export interface SnapshotBudget {
  maxElements: number;
  maxTextChars: number;
  maxNameChars: number;
}

/** Element boxes are viewport CSS px; the worker converts them to screenshot px. */
export interface PageSnapshot {
  url: string;
  title: string;
  viewport: Viewport;
  elements: ElementMark[];
  selection: string | null;
  text_excerpt: string | null;
  took_ms: number;
  /** Colour of the capture sentinel while the companion is hidden for a screenshot (see capture.ts). */
  sentinel?: [number, number, number];
  /** Viewport CSS boxes of every visible text field (not limited by the element budget): masked in the screenshot. */
  field_boxes: [number, number, number, number][];
  /** Milliseconds per phase (diagnostics: roots, candidates, checks, marks, text, fields). */
  phases: Record<string, number>;
  /** The scene the companion's stage is showing, if any: the brain may be asked to change it. */
  scene?: { scene_id: string; title: string } | null;
}

type State = NonNullable<ElementMark['states']>[number];

const HEADINGS = 'h1,h2,h3,h4,h5,h6,[role="heading"]';
const CONTROLS = [
  'button', 'input:not([type="hidden"])', 'select', 'textarea', 'summary', '[contenteditable=""]',
  '[contenteditable="true"]', '[role="button"]', '[role="checkbox"]', '[role="radio"]', '[role="switch"]',
  '[role="tab"]', '[role="menuitem"]', '[role="menuitemcheckbox"]', '[role="menuitemradio"]', '[role="option"]',
  '[role="combobox"]', '[role="textbox"]', '[role="searchbox"]', '[role="slider"]', '[role="spinbutton"]',
  '[role="treeitem"]', '[tabindex]:not([tabindex="-1"])',
].join(',');
const LINKS = 'a[href],[role="link"]';
const MEDIA = 'img,video,canvas,svg[role="img"],[role="img"]';
const LANDMARKS = [
  'main', 'nav', 'header', 'footer', 'aside', 'form', 'dialog', '[role="main"]', '[role="navigation"]',
  '[role="banner"]', '[role="contentinfo"]', '[role="complementary"]', '[role="search"]', '[role="dialog"]',
  '[role="alertdialog"]', '[role="region"][aria-label]', '[role="region"][aria-labelledby]',
].join(',');
/** Priority when the budget is tight: headings, then controls, links, media, landmarks. */
const GROUPS = [HEADINGS, CONTROLS, LINKS, MEDIA, LANDMARKS];
/** An element inside one of these is part of its parent's mark (the link or button already names it). */
const OUTER_ACTIONS = 'a[href],button,summary,[role="button"],[role="link"]';
/** Fields whose content is user data: their names come from labels only. */
const FIELDS = 'input,select,textarea,[contenteditable=""],[contenteditable="true"],[role="textbox"],[role="searchbox"],[role="combobox"]';
const NO_TEXT = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'TEXTAREA', 'SELECT', 'OPTION', 'INPUT', 'IFRAME', 'SVG']);
const INLINE = new Set(['A', 'SPAN', 'B', 'STRONG', 'EM', 'I', 'U', 'S', 'CODE', 'SMALL', 'LABEL', 'ABBR', 'MARK', 'SUB',
  'SUP', 'TIME', 'Q', 'CITE', 'KBD', 'VAR', 'DFN', 'BDI', 'BDO', 'FONT']);
const SECRET_PARAM = /token|secret|passw|auth|session|sig|key|code|ticket|otp|nonce/i;
/** Inputs that show something the user typed (everything but buttons, toggles, pickers and hidden ones). */
const NOT_TYPED = new Set(['button', 'submit', 'reset', 'image', 'checkbox', 'radio', 'range', 'color', 'file', 'hidden']);
const MAX_FIELD_BOXES = 200;
const HOST_TAG = 'CLICKYTABS-UI';
const TEXT_TIME_BOX_MS = 6;
/** Past this, remaining (lower-priority) candidates are checked for visibility but not hit-tested. */
const HIT_TEST_TIME_BOX_MS = 8;
const MIN_SIZE = 4;

// ------------------------------------------------------------------------------------------------ helpers

const collapse = (text: string): string => text.replace(/\s+/g, ' ').trim();

function clip(text: string, max: number): string {
  const clean = collapse(text);
  return clean.length > max ? `${clean.slice(0, max - 1)}…` : clean;
}

/** Drops the fragment and redacts secret-looking query values (reset links, OAuth codes, session ids). */
export function sanitizeUrl(href: string): string {
  try {
    const url = new URL(href);
    url.hash = '';
    for (const key of [...url.searchParams.keys()]) {
      if (SECRET_PARAM.test(key)) url.searchParams.set(key, 'redacted');
    }
    const text = url.toString();
    return text.length > 2048 ? text.slice(0, 2048) : text;
  } catch {
    return '';
  }
}

function roleOf(el: Element): string {
  const explicit = el.getAttribute('role')?.trim().split(/\s+/)[0];
  if (explicit) return explicit.slice(0, 40);
  switch (el.tagName) {
    case 'A':
      return 'link';
    case 'BUTTON':
    case 'SUMMARY':
      return 'button';
    case 'INPUT': {
      const type = (el.getAttribute('type') ?? 'text').toLowerCase();
      const byType: Record<string, string> = {
        checkbox: 'checkbox', radio: 'radio', range: 'slider', number: 'spinbutton', search: 'searchbox',
        button: 'button', submit: 'button', reset: 'button', image: 'button',
      };
      return byType[type] ?? 'textbox';
    }
    case 'SELECT':
      return 'combobox';
    case 'TEXTAREA':
      return 'textbox';
    case 'H1': case 'H2': case 'H3': case 'H4': case 'H5': case 'H6':
      return 'heading';
    case 'IMG': case 'SVG': case 'svg':
      return 'img';
    case 'VIDEO':
      return 'video';
    case 'CANVAS':
      return 'canvas';
    case 'MAIN':
      return 'main';
    case 'NAV':
      return 'navigation';
    case 'HEADER':
      return 'banner';
    case 'FOOTER':
      return 'contentinfo';
    case 'ASIDE':
      return 'complementary';
    case 'FORM':
      return 'form';
    case 'DIALOG':
      return 'dialog';
    default:
      return el.matches('[contenteditable=""],[contenteditable="true"]') ? 'textbox' : 'generic';
  }
}

function tagOf(el: Element): string {
  const tag = el.tagName.toLowerCase();
  if (tag !== 'input') return tag.slice(0, 40);
  return `input:${(el.getAttribute('type') ?? 'text').toLowerCase()}`.slice(0, 40);
}

function levelOf(el: Element): number | null {
  const fromTag = /^H([1-6])$/.exec(el.tagName);
  if (fromTag) return Number(fromTag[1]);
  const aria = Number(el.getAttribute('aria-level'));
  return el.getAttribute('role') === 'heading' && aria >= 1 && aria <= 6 ? aria : null;
}

function statesOf(el: Element): State[] {
  const states: State[] = [];
  const aria = (name: string): string | null => el.getAttribute(`aria-${name}`);
  if (el.matches(':disabled') || aria('disabled') === 'true') states.push('disabled');
  const input = el.tagName === 'INPUT' ? (el as HTMLInputElement) : null;
  if ((input && input.indeterminate) || aria('checked') === 'mixed') states.push('mixed');
  else if ((input && (input.type === 'checkbox' || input.type === 'radio') && input.checked) || aria('checked') === 'true') {
    states.push('checked');
  }
  if (aria('selected') === 'true') states.push('selected');
  const expanded = el.tagName === 'SUMMARY' ? String((el.parentElement as HTMLDetailsElement | null)?.open ?? '') : aria('expanded');
  if (expanded === 'true') states.push('expanded');
  else if (expanded === 'false') states.push('collapsed');
  if (aria('pressed') === 'true') states.push('pressed');
  if (el.matches(':required') || aria('required') === 'true') states.push('required');
  if (el.hasAttribute('readonly') || aria('readonly') === 'true') states.push('readonly');
  const current = aria('current');
  if (current && current !== 'false') states.push('current');
  return states;
}

/** A pragmatic slice of the accessible-name computation that never touches user-entered values. */
function accessibleName(el: Element, max: number): string {
  const doc = el.ownerDocument;
  const labelledBy = el.getAttribute('aria-labelledby');
  if (labelledBy) {
    const text = labelledBy.split(/\s+/).map((id) => doc.getElementById(id)?.textContent ?? '').join(' ');
    if (collapse(text)) return clip(text, max);
  }
  const label = el.getAttribute('aria-label');
  if (label && collapse(label)) return clip(label, max);
  if (el.tagName === 'INPUT') {
    const input = el as HTMLInputElement;
    const type = input.type.toLowerCase();
    if (type === 'button' || type === 'submit' || type === 'reset') return clip(input.value || type, max); // a caption
    if (type === 'image') return clip(input.alt || 'image button', max);
  }
  if (el.matches(FIELDS)) {
    const labels = (el as HTMLInputElement).labels;
    const fromLabels = labels ? [...labels].map((l) => l.textContent ?? '').join(' ') : '';
    const named = fromLabels || el.getAttribute('placeholder') || el.getAttribute('aria-placeholder')
      || el.getAttribute('title') || '';
    return clip(named, max);
  }
  if (el.tagName === 'IMG' || el.getAttribute('role') === 'img') {
    return clip(el.getAttribute('alt') ?? el.getAttribute('title') ?? '', max);
  }
  if (el.tagName.toLowerCase() === 'svg') return clip(el.querySelector('title')?.textContent ?? '', max);
  if (el.matches(LANDMARKS) && !el.matches(HEADINGS)) return clip(el.getAttribute('title') ?? '', max);
  const text = el.textContent ?? '';
  if (collapse(text)) return clip(text, max);
  // Icon/image-only links and buttons (logos, product cards): name them by what they contain.
  const inner = el.querySelector('img[alt]:not([alt=""]), [aria-label], svg title');
  const innerName = inner?.getAttribute('alt') ?? inner?.getAttribute('aria-label') ?? inner?.textContent ?? '';
  return clip(innerName || el.getAttribute('title') || '', max);
}

/**
 * Our own UI lives in a closed shadow root, so queries never return its insides; hit tests are retargeted to the
 * host element.
 */
function isOurs(el: Element | null): boolean {
  return el?.tagName === HOST_TAG;
}

/** Open shadow roots, nested ones included (closed roots, ours among them, are invisible by design). */
function openShadowRoots(doc: Document): ShadowRoot[] {
  const roots: ShadowRoot[] = [];
  const visit = (root: Document | ShadowRoot): void => {
    for (const el of root.querySelectorAll('*')) {
      const shadow = el.shadowRoot;
      if (shadow) {
        roots.push(shadow);
        visit(shadow);
      }
    }
  };
  visit(doc);
  return roots;
}

/** Subtrees that cannot be showing text: cheap attribute checks only (the visibility check covers the rest). */
function isHiddenSubtree(el: Element): boolean {
  if (el.hasAttribute('hidden') || el.getAttribute('aria-hidden') === 'true') return true;
  if (el.tagName === 'DIALOG') return !(el as HTMLDialogElement).open;
  if (el.hasAttribute('popover')) return !el.matches(':popover-open');
  const parent = el.parentElement;
  return parent?.tagName === 'DETAILS' && !(parent as HTMLDetailsElement).open && el.tagName !== 'SUMMARY';
}

function isRendered(el: Element): boolean {
  if (typeof el.checkVisibility === 'function') {
    return el.checkVisibility({ contentVisibilityAuto: true, opacityProperty: true, visibilityProperty: true });
  }
  const style = getComputedStyle(el);
  return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || '1') > 0;
}

/** Not covered by something else (a cookie banner, a modal backdrop): hit-test the middle of its visible part. */
function isOnTop(el: Element, rect: DOMRect, viewport: { width: number; height: number }): boolean {
  const x = (Math.max(rect.left, 0) + Math.min(rect.right, viewport.width)) / 2;
  const y = (Math.max(rect.top, 0) + Math.min(rect.bottom, viewport.height)) / 2;
  const root = el.getRootNode() as Document | ShadowRoot;
  const hit = (typeof root.elementFromPoint === 'function' ? root : el.ownerDocument).elementFromPoint(x, y);
  if (!hit) return false;
  return hit === el || el.contains(hit) || hit.contains(el) || isOurs(hit);
}

// ------------------------------------------------------------------------------------------------ text

function visibleText(doc: Document, roots: readonly (Document | ShadowRoot)[], max: number,
  viewport: { width: number; height: number }): string {
  if (max <= 0) return '';
  const started = performance.now();
  const visible = new Map<Element, boolean>();
  const blockOf = (el: Element): Element => {
    let node: Element = el;
    while (INLINE.has(node.tagName) && node.parentElement) node = node.parentElement;
    return node;
  };
  const isVisible = (el: Element): boolean => {
    let known = visible.get(el);
    if (known === undefined) {
      const r = el.getBoundingClientRect();
      known = r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < viewport.height
        && r.left < viewport.width && isRendered(el);
      visible.set(el, known);
    }
    return known;
  };
  let out = '';
  let lastBlock: Element | null = null;
  for (const root of roots) {
    const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
      acceptNode: (node) => {
        if (node.nodeType === Node.TEXT_NODE) return NodeFilter.FILTER_ACCEPT;
        const el = node as Element;
        return NO_TEXT.has(el.tagName) || el.tagName === HOST_TAG || el.tagName === 'svg'
          || el.hasAttribute('contenteditable') || isHiddenSubtree(el)
          ? NodeFilter.FILTER_REJECT
          : NodeFilter.FILTER_SKIP;
      },
    });
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (out.length >= max || performance.now() - started > TEXT_TIME_BOX_MS) return out.slice(0, max);
      const text = collapse(node.nodeValue ?? '');
      const parent = node.parentElement;
      if (!text || !parent || !isVisible(parent)) continue;
      const block = blockOf(parent);
      out += out ? (block === lastBlock ? ' ' : '\n') : '';
      out += text;
      lastBlock = block;
    }
  }
  return out.slice(0, max);
}

function fieldBoxes(roots: readonly (Document | ShadowRoot)[], viewport: { width: number; height: number }):
  [number, number, number, number][] {
  const boxes: [number, number, number, number][] = [];
  for (const root of roots) {
    for (const el of root.querySelectorAll('input, textarea')) {
      if (boxes.length >= MAX_FIELD_BOXES) return boxes;
      if (el.tagName === 'INPUT' && NOT_TYPED.has((el as HTMLInputElement).type.toLowerCase())) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1 || r.bottom <= 0 || r.right <= 0 || r.top >= viewport.height
        || r.left >= viewport.width || !isRendered(el)) continue;
      boxes.push([r.left, r.top, r.width, r.height]);
    }
  }
  return boxes;
}

function selectedText(doc: Document): string | null {
  const active = doc.activeElement;
  if (active && active.matches('input,textarea')) return null; // a field's selection is its value
  const text = collapse(doc.getSelection?.()?.toString() ?? '');
  return text ? text.slice(0, 2000) : null;
}

// ------------------------------------------------------------------------------------------------ snapshot

/** Takes the snapshot and registers its marks (the registry is reset: ids restart at e1 for every turn). */
export function snapshotPage(doc: Document, registry: ElementRegistry, budget: SnapshotBudget): PageSnapshot {
  const started = performance.now();
  const phases: Record<string, number> = {};
  let lapStart = started;
  const lap = (name: string): void => {
    const now = performance.now();
    phases[name] = Math.round((now - lapStart) * 10) / 10;
    lapStart = now;
  };
  const win = doc.defaultView ?? window;
  const viewport = { width: win.innerWidth, height: win.innerHeight };
  doc.documentElement?.getBoundingClientRect(); // flush the page's own pending style/layout (reported separately)
  lap('layout');
  const roots: (Document | ShadowRoot)[] = [doc, ...openShadowRoots(doc)];
  lap('roots');

  // 1. Candidates on screen, cheapest checks first.
  interface Candidate { el: Element; rect: DOMRect; group: number; order: number }
  const candidates: Candidate[] = [];
  const seen = new Set<Element>();
  GROUPS.forEach((selector, group) => {
    for (const root of roots) {
      for (const el of root.querySelectorAll(selector)) {
        if (seen.has(el)) continue; // already in a higher-priority group
        seen.add(el);
        const rect = el.getBoundingClientRect();
        if (rect.width < MIN_SIZE || rect.height < MIN_SIZE) continue;
        if (rect.bottom <= 0 || rect.right <= 0 || rect.top >= viewport.height || rect.left >= viewport.width) continue;
        if (group > 0 && el.parentElement?.closest(OUTER_ACTIONS)) continue; // part of an enclosing link or button
        candidates.push({ el, rect, group, order: candidates.length });
      }
    }
  });
  candidates.sort((a, b) => a.group - b.group || a.order - b.order);
  lap('candidates');

  // 2. Expensive checks in priority order until the budget is full.
  const accepted: Candidate[] = [];
  const checksStarted = performance.now();
  for (const candidate of candidates) {
    if (accepted.length >= budget.maxElements) break;
    if (!isRendered(candidate.el)) continue;
    const hitTest = performance.now() - checksStarted < HIT_TEST_TIME_BOX_MS;
    if (!hitTest || isOnTop(candidate.el, candidate.rect, viewport)) accepted.push(candidate);
  }
  lap('checks');

  // 3. Reading order (rows top to bottom, then left to right), then ids.
  accepted.sort((a, b) => Math.round(a.rect.top / 12) - Math.round(b.rect.top / 12) || a.rect.left - b.rect.left);
  registry.reset();
  const elements: ElementMark[] = accepted.map(({ el, rect }) => {
    const mark: ElementMark = {
      id: registry.register(el),
      role: roleOf(el),
      name: accessibleName(el, budget.maxNameChars),
      tag: tagOf(el),
      bbox: [rect.left, rect.top, rect.width, rect.height],
    };
    const level = levelOf(el);
    if (level !== null) mark.level = level;
    const states = statesOf(el);
    if (states.length) mark.states = states;
    return mark;
  });
  lap('marks');

  const text = visibleText(doc, roots, budget.maxTextChars, viewport);
  lap('text');
  const fields = fieldBoxes(roots, viewport);
  lap('fields');
  return {
    url: sanitizeUrl(doc.location?.href ?? ''),
    title: clip(doc.title, 500),
    viewport: { ...viewport, dpr: win.devicePixelRatio || 1, scroll_x: win.scrollX, scroll_y: win.scrollY },
    elements,
    selection: selectedText(doc),
    text_excerpt: text || null,
    field_boxes: fields,
    phases,
    took_ms: Math.round((performance.now() - started) * 10) / 10,
  };
}
