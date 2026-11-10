// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest';

import { isAllowed, parseSwRequest, parseTabMessage } from '@/lib/messages';
import { ElementRegistry } from '@/lib/overlay/targets';
import { sanitizeUrl, type SnapshotBudget, snapshotPage } from '@/lib/page-context/snapshot';

const BUDGET: SnapshotBudget = { maxElements: 80, maxTextChars: 2000, maxNameChars: 80 };
const VIEWPORT = { width: innerWidth, height: innerHeight };

interface Box {
  el: Element;
  x: number;
  y: number;
  w: number;
  h: number;
}
let boxes: Box[] = [];

/** happy-dom has no layout: give elements rects, and hit-test against them (last placed is on top). */
function place(el: Element, x: number, y: number, w: number, h: number): Element {
  el.getBoundingClientRect = () =>
    ({ x, y, width: w, height: h, left: x, top: y, right: x + w, bottom: y + h, toJSON: () => ({}) }) as DOMRect;
  boxes.push({ el, x, y, w, h });
  return el;
}

function installHitTest(): void {
  document.elementFromPoint = (px: number, py: number) => {
    for (const box of [...boxes].reverse()) {
      if (px >= box.x && px <= box.x + box.w && py >= box.y && py <= box.y + box.h) return box.el;
    }
    return document.body;
  };
}

function setup(html: string): void {
  boxes = [];
  document.body.innerHTML = html;
  place(document.body, 0, 0, VIEWPORT.width, VIEWPORT.height);
  installHitTest();
}

const $ = (selector: string): Element => document.querySelector(selector)!;

afterEach(() => {
  document.body.innerHTML = '';
  window.getSelection()?.removeAllRanges();
});

describe('DOM set-of-marks', () => {
  it('names elements from labels, ARIA and text, never from what the user typed', () => {
    setup(`
      <h2 id="h">Checkout</h2>
      <label for="email">Email address</label><input id="email" value="me@private.example">
      <label for="pw">Password</label><input id="pw" type="password" value="hunter2">
      <input id="go" type="submit" value="Place order">
      <select id="state" aria-label="State"><option selected>California</option></select>
      <textarea id="note" placeholder="Note to seller">my secret draft</textarea>
      <div id="editor" contenteditable="true" aria-label="Message">typed private words</div>
      <a id="help" href="/help" aria-labelledby="h">?</a>
      <img id="logo" alt="Shop logo">
      <button id="cart" aria-expanded="false" disabled>Cart</button>`);
    for (const [selector, y] of [['#h', 10], ['#email', 40], ['#pw', 70], ['#go', 100], ['#state', 130], ['#note', 160],
      ['#editor', 200], ['#help', 240], ['#logo', 270], ['#cart', 320]] as const) {
      place($(selector), 20, y, 200, 24);
    }
    const registry = new ElementRegistry();
    const snapshot = snapshotPage(document, registry, BUDGET);
    const byId = Object.fromEntries(snapshot.elements.map((m) => [(registry.get(m.id) as HTMLElement).id, m]));

    expect(byId.h).toMatchObject({ role: 'heading', name: 'Checkout', level: 2 });
    expect(byId.email).toMatchObject({ role: 'textbox', name: 'Email address', tag: 'input:text' });
    expect(byId.pw).toMatchObject({ role: 'textbox', name: 'Password', tag: 'input:password' });
    expect(byId.go).toMatchObject({ role: 'button', name: 'Place order' });
    expect(byId.state).toMatchObject({ role: 'combobox', name: 'State' });
    expect(byId.note).toMatchObject({ role: 'textbox', name: 'Note to seller' });
    expect(byId.editor).toMatchObject({ role: 'textbox', name: 'Message' });
    expect(byId.help).toMatchObject({ role: 'link', name: 'Checkout' });
    expect(byId.logo).toMatchObject({ role: 'img', name: 'Shop logo' });
    expect(byId.cart).toMatchObject({ role: 'button', name: 'Cart', states: ['disabled', 'collapsed'] });

    const everything = JSON.stringify(snapshot);
    for (const secret of ['me@private.example', 'hunter2', 'California', 'my secret draft', 'typed private words']) {
      expect(everything).not.toContain(secret);
    }
  });

  it('keeps only what is on screen, rendered and not covered', () => {
    setup(`
      <button id="ok">Visible</button>
      <button id="gone" style="display:none">Hidden</button>
      <button id="below">Below the fold</button>
      <button id="tiny">x</button>
      <button id="covered">Under the banner</button>
      <div id="banner">Cookie banner</div>`);
    place($('#ok'), 10, 10, 100, 30);
    place($('#gone'), 10, 50, 100, 30);
    place($('#below'), 10, VIEWPORT.height + 50, 100, 30);
    place($('#tiny'), 10, 90, 2, 2);
    place($('#covered'), 10, 400, 100, 30);
    place($('#banner'), 0, 380, VIEWPORT.width, 80); // on top of #covered
    const registry = new ElementRegistry();
    const names = snapshotPage(document, registry, BUDGET).elements.map((m) => m.name);
    expect(names).toEqual(['Visible']);
  });

  it('respects the element budget, headings first, then reads top-to-bottom with ids e1..eN', () => {
    setup(`${Array.from({ length: 300 }, (_, i) => `<a href="#${i}">Link ${i}</a>`).join('')}<h1>Title</h1>`);
    document.querySelectorAll('a').forEach((a, i) => place(a, (i % 10) * 90, 60 + Math.floor(i / 10) * 20, 80, 16));
    place($('h1'), 10, 10, 400, 40);
    const registry = new ElementRegistry();
    const snapshot = snapshotPage(document, registry, { ...BUDGET, maxElements: 25 });
    expect(snapshot.elements).toHaveLength(25);
    expect(snapshot.elements[0]).toMatchObject({ id: 'e1', role: 'heading', name: 'Title' });
    expect(snapshot.elements.map((m) => m.id)).toEqual(Array.from({ length: 25 }, (_, i) => `e${i + 1}`));
    expect(registry.get('e2')?.textContent).toBe('Link 0');
    const tops = snapshot.elements.map((m) => m.bbox[1]);
    expect([...tops].sort((a, b) => a - b)).toEqual(tops);
  });

  it('marks the parent action, not the image inside it, and finds open shadow roots', () => {
    setup('<a id="card" href="/p"><img alt="Product photo"></a><div id="host"></div>');
    place($('#card'), 10, 10, 200, 200);
    place($('#card img'), 10, 10, 200, 200);
    const shadow = $('#host').attachShadow({ mode: 'open' });
    shadow.innerHTML = '<button>Inside shadow</button>';
    place(shadow.querySelector('button')!, 300, 10, 120, 30);
    const names = snapshotPage(document, new ElementRegistry(), BUDGET).elements.map((m) => `${m.role}:${m.name}`);
    expect(names).toContain('link:Product photo'); // an image-only link is named by its image
    expect(names).not.toContain('img:Product photo');
    expect(names).toContain('button:Inside shadow');
  });

  it('collects visible text within budget, without scripts, fields or editable content', () => {
    setup(`
      <p id="a">First paragraph of the page.</p>
      <p id="b">Second <b>bold</b> paragraph.</p>
      <script>var hidden = "script text";</script>
      <textarea id="t">draft text</textarea>
      <div id="e" contenteditable="true">editable words</div>
      <p id="far">Way below the fold.</p>`);
    place($('#a'), 0, 0, 400, 20);
    place($('#b'), 0, 30, 400, 20);
    place($('#b b'), 60, 30, 40, 20);
    place($('#t'), 0, 60, 200, 40);
    place($('#e'), 0, 110, 200, 40);
    place($('#far'), 0, VIEWPORT.height + 100, 400, 20);
    const snapshot = snapshotPage(document, new ElementRegistry(), BUDGET);
    expect(snapshot.text_excerpt).toBe('First paragraph of the page.\nSecond bold paragraph.');
    const tight = snapshotPage(document, new ElementRegistry(), { ...BUDGET, maxTextChars: 10 });
    expect(tight.text_excerpt).toBe('First para');
  });

  it('includes the selection unless the focus is in a field', () => {
    setup('<p id="p">Select these words please</p><input id="i">');
    place($('#p'), 0, 0, 400, 20);
    const range = document.createRange();
    range.selectNodeContents($('#p'));
    window.getSelection()!.addRange(range);
    expect(snapshotPage(document, new ElementRegistry(), BUDGET).selection).toBe('Select these words please');
    ($('#i') as HTMLInputElement).focus();
    expect(snapshotPage(document, new ElementRegistry(), BUDGET).selection).toBeNull();
  });

  it('reports every visible text field for masking, beyond the element budget', () => {
    setup(`<input id="a"><textarea id="b"></textarea><input id="c" type="checkbox"><input id="d" type="hidden">
      <input id="e" type="password"><input id="f" style="display:none">`);
    for (const [selector, x] of [['#a', 0], ['#b', 100], ['#c', 200], ['#d', 300], ['#e', 400], ['#f', 500]] as const) {
      place($(selector), x, 10, 80, 20);
    }
    const snapshot = snapshotPage(document, new ElementRegistry(), { ...BUDGET, maxElements: 0 });
    expect(snapshot.elements).toEqual([]);
    expect(snapshot.field_boxes).toEqual([[0, 10, 80, 20], [100, 10, 80, 20], [400, 10, 80, 20]]);
  });

  it('redacts secret-looking URL parameters and drops the fragment', () => {
    expect(sanitizeUrl('https://shop.test/search?q=shoes&token=abc&oauth_code=xyz#section')).toBe(
      'https://shop.test/search?q=shoes&token=redacted&oauth_code=redacted');
    expect(sanitizeUrl('not a url')).toBe('');
  });
});

describe('page-context messages', () => {
  it('validates snapshot budgets and capture results', () => {
    const budget = { maxElements: 80, maxTextChars: 2000, maxNameChars: 80 };
    expect(parseTabMessage({ type: 'context.snapshot', budget, hideUi: true })).toEqual(
      { type: 'context.snapshot', budget, hideUi: true });
    expect(parseTabMessage({ type: 'context.snapshot', budget: { ...budget, maxElements: 5000 }, hideUi: true })).toBeNull();
    expect(parseTabMessage({ type: 'context.snapshot', budget })).toBeNull();
    expect(parseTabMessage({ type: 'context.restore', image: null })).toEqual({ type: 'context.restore', image: null });
    expect(parseTabMessage({ type: 'context.restore', image: { width: 1024, height: 640 } })).toEqual(
      { type: 'context.restore', image: { width: 1024, height: 640 } });
    expect(parseTabMessage({ type: 'context.restore', image: { width: -1, height: 640 } })).toBeNull();
  });

  it('keeps the page-context debug hooks for extension pages only', () => {
    expect(parseSwRequest({ type: 'debug.pageContext', tabId: 3 })).toEqual({ type: 'debug.pageContext', tabId: 3 });
    expect(isAllowed('debug.pageContext', 'content-top')).toBe(false);
    expect(isAllowed('debug.marks', 'extension-page')).toBe(true);
  });
});
