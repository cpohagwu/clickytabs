/**
 * Sanitizer for declarative scenes (kind "svg" and "html"), run in the sandbox page before anything is shown.
 *
 * Allowlist-based: unknown elements are unwrapped (their children stay), dangerous ones are dropped with their
 * content, and every attribute is checked. The sandbox CSP already blocks scripts other than the harness, inline
 * handlers and all network access; this pass also removes everything that could *navigate* the sandbox frame or
 * hide a script for a later bug: links, forms, meta refresh, base, frames, script-bearing SVG animation targets.
 */

const DROP_WITH_CONTENT = new Set([
  'script', 'noscript', 'iframe', 'frame', 'frameset', 'object', 'embed', 'applet', 'form', 'input', 'button',
  'select', 'textarea', 'option', 'meta', 'base', 'link', 'template', 'slot', 'portal', 'foreignobject', 'audio',
  'video', 'source', 'track', 'canvas', 'dialog', 'fencedframe', 'math', 'handler', 'listener', 'feimage',
]);

const HTML_ALLOWED = new Set([
  'div', 'span', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'dl', 'dt', 'dd', 'table', 'thead',
  'tbody', 'tfoot', 'tr', 'th', 'td', 'caption', 'colgroup', 'col', 'strong', 'em', 'b', 'i', 'u', 's', 'small',
  'mark', 'sub', 'sup', 'code', 'pre', 'kbd', 'samp', 'var', 'blockquote', 'q', 'cite', 'abbr', 'time', 'figure',
  'figcaption', 'section', 'article', 'aside', 'header', 'footer', 'main', 'nav', 'hr', 'br', 'wbr', 'img', 'style',
  'details', 'summary', 'progress', 'meter', 'ruby', 'rt', 'rp', 'bdi', 'bdo', 'del', 'ins', 'center', 'font',
]);

const SVG_ALLOWED = new Set([
  'svg', 'g', 'defs', 'symbol', 'use', 'title', 'desc', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon',
  'path', 'text', 'tspan', 'textpath', 'lineargradient', 'radialgradient', 'stop', 'pattern', 'clippath', 'mask',
  'marker', 'filter', 'style', 'image', 'animate', 'animatetransform', 'animatemotion', 'mpath', 'set', 'switch',
  'fegaussianblur', 'feoffset', 'feblend', 'fecolormatrix', 'fecomposite', 'feflood', 'femerge', 'femergenode',
  'fedropshadow', 'femorphology', 'feturbulence', 'fedisplacementmap', 'fecomponenttransfer', 'fefuncr', 'fefuncg',
  'fefuncb', 'fefunca', 'fespecularlighting', 'fediffuselighting', 'fepointlight', 'fedistantlight', 'fespotlight',
  'fetile', 'feconvolvematrix',
]);

const ANIMATION = new Set(['animate', 'animatetransform', 'animatemotion', 'set']);
const URL_ATTRIBUTES = new Set(['href', 'xlink:href', 'src', 'action', 'formaction', 'background', 'poster', 'srcset',
  'ping', 'data', 'codebase', 'cite', 'longdesc', 'usemap', 'manifest']);

export interface SanitizeResult {
  /** The cleaned root (an <svg> for kind "svg", a <div> holding the fragment for "html"), or null if nothing is left. */
  root: Element | null;
  /** What was removed, for the repair prompt and the logs ("<script>", "onclick", "href=https:…"). */
  removed: string[];
}

function note(removed: string[], item: string): void {
  if (removed.length < 24 && !removed.includes(item)) removed.push(item);
}

function isSafeImage(value: string): boolean {
  return /^data:image\/(png|jpe?g|gif|webp|svg\+xml);/i.test(value.trim()) && value.length < 2_000_000;
}

function cleanAttributes(element: Element, tag: string, removed: string[]): void {
  for (const attribute of Array.from(element.attributes)) {
    const name = attribute.name.toLowerCase();
    const value = attribute.value;
    const squashed = value.replace(/[\s\u0000-\u001f]+/g, '').toLowerCase();
    let drop = false;
    if (name.startsWith('on')) drop = true;                                    // event handlers
    else if (squashed.includes('javascript:') || squashed.includes('vbscript:')) drop = true;
    else if (name === 'style' && /expression\(|@import|behavior:/i.test(value)) drop = true;
    else if (URL_ATTRIBUTES.has(name)) {
      // Fragment references keep markers, gradients, <use> and textPath working; images may be data: URLs.
      const fragment = value.trim().startsWith('#');
      const image = (tag === 'img' || tag === 'image') && (name === 'src' || name === 'href' || name === 'xlink:href');
      drop = !(fragment && (name === 'href' || name === 'xlink:href')) && !(image && isSafeImage(value));
    } else if (ANIMATION.has(tag) && (name === 'attributename')
      && /^(href|xlink:href|src|on|style)/i.test(value.trim())) {
      drop = true;                                                               // animating a link or a handler
    } else if (name === 'http-equiv' || name === 'srcdoc' || name === 'formtarget' || name === 'target') {
      drop = true;
    }
    if (drop) {
      element.removeAttribute(attribute.name);
      note(removed, `${name}${URL_ATTRIBUTES.has(name) ? `=${value.slice(0, 40)}` : ''}`);
    }
  }
}

function cleanTree(node: Element, inSvg: boolean, removed: string[]): void {
  for (const child of Array.from(node.children)) {
    const tag = child.localName.toLowerCase();
    const svg = inSvg || tag === 'svg';
    if (DROP_WITH_CONTENT.has(tag)) {
      child.remove();
      note(removed, `<${tag}>`);
      continue;
    }
    const allowed = svg ? SVG_ALLOWED.has(tag) || (tag === 'svg') : HTML_ALLOWED.has(tag) || tag === 'svg';
    if (!allowed) {
      // Unknown or unwanted wrapper (a, area, custom elements, html/body/head): keep what's inside.
      cleanTree(child, svg, removed);
      child.replaceWith(...Array.from(child.childNodes));
      note(removed, `<${tag}>`);
      continue;
    }
    cleanAttributes(child, tag, removed);
    if (tag === 'img' && !child.hasAttribute('src')) {
      child.remove();
      continue;
    }
    cleanTree(child, svg, removed);
  }
}

/** A viewBox from width/height when it has none, so the drawing can scale. */
function ensureViewBox(svg: Element): void {
  if (svg.hasAttribute('viewBox')) return;
  const width = Number.parseFloat(svg.getAttribute('width') ?? '');
  const height = Number.parseFloat(svg.getAttribute('height') ?? '');
  if (width > 0 && height > 0) svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
}

/** Lets the scene's <svg> scale to the whole stage. */
function fitSvg(svg: Element): void {
  ensureViewBox(svg);
  svg.setAttribute('width', '100%');
  svg.setAttribute('height', '100%');
  if (!svg.hasAttribute('preserveAspectRatio')) svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
}

export function sanitizeScene(kind: 'svg' | 'html', markup: string, parser: DOMParser = new DOMParser()): SanitizeResult {
  const removed: string[] = [];
  // HTML parsing for both: it understands inline SVG, never runs scripts and loads nothing (inert document).
  const parsed = parser.parseFromString(`<!doctype html><html><head></head><body>${markup}</body></html>`, 'text/html');
  const body = parsed.body;
  // <style> inside <head> (if the model wrote a full document) moves along with the body content.
  for (const style of Array.from(parsed.head.querySelectorAll('style'))) body.prepend(style);
  cleanTree(body, false, removed);
  if (kind === 'svg') {
    const svg = body.querySelector('svg');
    if (!svg) return { root: null, removed };
    fitSvg(svg);
    return { root: svg, removed };
  }
  for (const svg of Array.from(body.querySelectorAll('svg'))) ensureViewBox(svg);
  const wrapper = parsed.createElement('div');
  wrapper.className = 'cc-scene-html';
  wrapper.append(...Array.from(body.childNodes));
  return { root: wrapper.childNodes.length ? wrapper : null, removed };
}
