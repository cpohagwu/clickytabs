/**
 * E2E: the companion UI and the pointer overlay on pages that fight back.
 *
 * The fixture page combines what real sites do to injected widgets: global !important resets aimed at every element
 * (and at our host by name), a transformed/filtered body, a strict CSP (nonce-only scripts and styles, Trusted
 * Types), a page-opened modal <dialog>, a page popover and element fullscreen. The shadow root is closed, so
 * geometry and hit-testing come from the content script's `debug.state` probe, and paint is verified from real
 * screenshots, decoded in an extension page, by counting the companion's neon pixels.
 */
import type { BrowserContext, Page, TestInfo, Worker } from '@playwright/test';

import { debugState, expect, openFixture, pair, popup, test } from './fixtures';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const chrome: any;

const STRICT_CSP = [
  "default-src 'none'",
  "script-src 'nonce-e2e'",
  "style-src 'nonce-e2e'",
  'img-src data:',
  "require-trusted-types-for 'script'",
  "trusted-types 'none'",
].join('; ');

const GREY_SVG =
  "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='120'%3E%3Crect width='160' " +
  "height='120' fill='%23888'/%3E%3C/svg%3E";

const HOSTILE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>hostile</title>
<style nonce="e2e">
  /* What careless or hostile pages do to injected widgets. None of it may reach the companion. */
  *, *::before, *::after {
    font: 700 22px/2.4 "Comic Sans MS", cursive !important;
    color: #0a0 !important;
    letter-spacing: 3px !important;
    box-sizing: content-box !important;
    cursor: crosshair !important;
  }
  html { background: #fff8e6 !important; }
  body { margin: 0; padding: 24px; transform: translateZ(0); filter: contrast(1); }
  body > *:not(main):not(dialog):not([popover]):not(script) {
    display: none !important; visibility: hidden !important; opacity: 0 !important; transform: scale(0) !important;
  }
  clickytabs-ui { display: none !important; visibility: hidden !important; contain: strict !important; }
  div, canvas, button, section, form, input, iframe { display: none !important; }
  [popover]:not(#page-popover) { display: none !important; }
  #pic { display: block; width: 160px; height: 120px; }
  #stage { background: #fff8e6; }
  dialog::backdrop { background: rgb(0 0 0 / 0.55); }
</style>
</head>
<body>
<main id="stage">
  <h1>Hostile fixture</h1>
  <p><a id="cta" href="#buy">Buy the thing</a> · <a id="open-modal" href="#m">modal</a> ·
     <a id="open-popover" href="#p">popover</a> · <a id="go-fullscreen" href="#f">fullscreen</a></p>
  <p id="copy">This paragraph is long enough for the demo tour to underline it as the important part.</p>
  <img id="pic" alt="placeholder" src="${GREY_SVG}">
</main>
<dialog id="modal"><p>A page-opened modal dialog</p><a id="close-modal" href="#c">close</a></dialog>
<p popover="manual" id="page-popover">A page popover</p>
<script nonce="e2e">
  let keys = 0;
  document.addEventListener('keydown', () => { keys += 1; document.title = 'keys:' + keys; });
  const on = (id, fn) => document.getElementById(id).addEventListener('click', (e) => { e.preventDefault(); fn(); });
  on('open-modal', () => document.getElementById('modal').showModal());
  on('close-modal', () => document.getElementById('modal').close());
  on('open-popover', () => document.getElementById('page-popover').showPopover());
  on('go-fullscreen', () => document.getElementById('stage').requestFullscreen());
</script>
</body>
</html>`;

const PLAIN = `<!doctype html><title>plain</title><body style="margin:0;background:#fff">
<h1>Plain page</h1><p><a href="#x">A link</a></p>
<p>Some paragraph text that is long enough to be underlined during the tour.</p></body>`;

interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

function grow(box: Box, by: number): Box {
  return { x: Math.max(0, box.x - by), y: Math.max(0, box.y - by), width: box.width + 2 * by, height: box.height + 2 * by };
}

/** Share of pixels in the shot that are companion neon (saturated cyan/violet/magenta: strong blue channel). */
async function neonFraction(ui: Page, png: Buffer): Promise<number> {
  return ui.evaluate(async (b64) => {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/png' }));
    const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
    const ctx = canvas.getContext('2d')!;
    ctx.drawImage(bitmap, 0, 0);
    const { data } = ctx.getImageData(0, 0, bitmap.width, bitmap.height);
    let neon = 0;
    for (let i = 0; i < data.length; i += 4) {
      const r = data[i]!;
      const g = data[i + 1]!;
      const b = data[i + 2]!;
      const max = Math.max(r, g, b);
      const saturation = max === 0 ? 0 : (max - Math.min(r, g, b)) / max;
      if (saturation > 0.45 && b > 140 && b >= g * 0.85) neon += 1;
    }
    return neon / (data.length / 4);
  }, png.toString('base64'));
}

/** Screenshot kept in the test's output folder (for visual review) and attached to the report. */
async function snap(page: Page, testInfo: TestInfo, name: string, clip?: Box): Promise<Buffer> {
  const body = await page.screenshot({ clip, path: testInfo.outputPath(`${name}.png`) });
  await testInfo.attach(name, { body, contentType: 'image/png' });
  return body;
}

async function captionContains(ui: Page, tabId: number, text: string): Promise<void> {
  await expect.poll(async () => (await debugState(ui, tabId)).caption, { timeout: 30_000 }).toContain(text);
}

/** Paired, so the orb shows its live (saturated) colours rather than the dimmed "not connected" look. */
async function openHostile(context: BrowserContext, sw: Worker) {
  await pair(context, sw);
  const opened = await openFixture(context, {
    path: 'hostile',
    html: HOSTILE,
    headers: { 'content-security-policy': STRICT_CSP },
  });
  const ui = await popup(context);
  await opened.page.bringToFront(); // rAF only runs in the visible tab
  await expect.poll(async () => (await debugState(ui, opened.tabId)).layout?.popoverOpen).toBe(true);
  return { ...opened, ui };
}

async function runDemo(ui: Page, tabId: number): Promise<void> {
  expect(await ui.evaluate((id) => chrome.runtime.sendMessage({ type: 'demo.run', tabId: id }), tabId)).toEqual({ ok: true });
}

test('renders untouched on a hostile strict-CSP page, and the demo draws on the real elements', async ({ context, sw }, testInfo) => {
  const { page, tabId, ui } = await openHostile(context, sw);
  const viewport = page.viewportSize()!;
  const { layout } = await debugState(ui, tabId);
  expect(layout).not.toBeNull();
  expect(layout!.orb).toMatchObject({ width: 56, height: 56 });
  expect(layout!.orb.x).toBeCloseTo(viewport.width - 24 - 56, 0);
  expect(layout!.orb.y).toBeCloseTo(viewport.height - 24 - 56, 0);
  expect(layout!.hitAtOrb).toBe('orb');
  expect(layout!.canvas.cssWidth).toBe(layout!.viewport.width);
  expect(layout!.canvas.cssHeight).toBe(layout!.viewport.height);
  expect(await neonFraction(ui, await page.screenshot({ clip: layout!.orb }))).toBeGreaterThan(0.3);

  await runDemo(ui, tabId);
  await expect.poll(async () => (await debugState(ui, tabId)).annotations, { timeout: 15_000 }).toBeGreaterThanOrEqual(2);
  const heading = (await page.locator('h1').boundingBox())!;
  const shot = await snap(page, testInfo, 'heading-circled', grow(heading, 24));
  expect(await neonFraction(ui, shot)).toBeGreaterThan(0.005);
  await snap(page, testInfo, 'demo-1-heading');
  await captionContains(ui, tabId, 'with an arrow.');
  await page.waitForTimeout(700);
  await snap(page, testInfo, 'demo-2-arrow');
  await captionContains(ui, tabId, 'sketch freehand.');
  await page.waitForTimeout(700);
  await snap(page, testInfo, 'demo-3-sketch');

  await expect.poll(async () => (await debugState(ui, tabId)).caption, { timeout: 40_000 }).toContain('Cleaning up now.');
  await expect.poll(async () => (await debugState(ui, tabId)).orbState).toBe('idle');
  await expect.poll(async () => (await debugState(ui, tabId)).annotations).toBe(0);
});

test('stays above a page modal dialog, page popovers and fullscreen', async ({ context, sw }, testInfo) => {
  const { page, tabId, ui } = await openHostile(context, sw);

  // Modal dialog: the companion is re-promoted above it and keeps painting (the page makes it inert: documented).
  await page.click('#open-modal');
  await expect(page.locator('#modal')).toBeVisible();
  await expect.poll(async () => (await debugState(ui, tabId)).layout?.popoverOpen).toBe(true);
  let layout = (await debugState(ui, tabId)).layout!;
  expect(await neonFraction(ui, await page.screenshot({ clip: layout.orb }))).toBeGreaterThan(0.3);
  testInfo.annotations.push({ type: 'hit-test with a modal open', description: layout.hitAtOrb });
  await runDemo(ui, tabId);
  await expect.poll(async () => (await debugState(ui, tabId)).annotations, { timeout: 15_000 }).toBeGreaterThanOrEqual(2);
  const heading = (await page.locator('h1').boundingBox())!;
  const overModal = await snap(page, testInfo, 'annotation-over-modal', grow(heading, 24));
  await snap(page, testInfo, 'modal');
  expect(await neonFraction(ui, overModal)).toBeGreaterThan(0.005);
  await page.click('#close-modal');
  await expect(page.locator('#modal')).toBeHidden();

  // Page popover opened after the companion: the toggle event re-promotes the companion above it.
  await page.click('#open-popover');
  await expect(page.locator('#page-popover')).toBeVisible();
  await expect.poll(async () => (await debugState(ui, tabId)).layout?.hitAtOrb).toBe('orb');
  layout = (await debugState(ui, tabId)).layout!;
  expect(layout.popoverOpen).toBe(true);
  expect(await neonFraction(ui, await page.screenshot({ clip: layout.orb }))).toBeGreaterThan(0.3);

  // Element fullscreen: fullscreenchange re-promotes the companion above the fullscreen element, so it keeps
  // painting. Like a modal, fullscreen makes everything outside the element inert: clicks pass through (documented).
  await page.click('#go-fullscreen');
  await page.waitForFunction(() => document.fullscreenElement !== null);
  await expect.poll(async () => (await debugState(ui, tabId)).layout?.popoverOpen).toBe(true);
  layout = (await debugState(ui, tabId)).layout!;
  testInfo.annotations.push({ type: 'hit-test in fullscreen', description: layout.hitAtOrb });
  await snap(page, testInfo, 'fullscreen');
  expect(await neonFraction(ui, await page.screenshot({ clip: layout.orb }))).toBeGreaterThan(0.3);
  await page.evaluate(() => document.exitFullscreen());
});

test('HUD: click the orb, type, read the streamed reply; keystrokes never reach the page', async ({ context, sw }, testInfo) => {
  const { page, tabId, ui } = await openHostile(context, sw);
  await expect.poll(async () => (await debugState(ui, tabId)).conn).toBe('connected');

  const { orb } = (await debugState(ui, tabId)).layout!;
  await page.mouse.click(orb.x + orb.width / 2, orb.y + orb.height / 2);
  await expect.poll(async () => (await debugState(ui, tabId)).hudOpen).toBe(true);

  const title = await page.title();
  await page.keyboard.type('hello hud');
  await page.keyboard.press('Enter');
  await expect.poll(async () => (await debugState(ui, tabId)).caption).toContain('You said: hello hud.');
  await expect.poll(async () => (await debugState(ui, tabId)).orbState).toBe('idle');
  expect(await page.title()).toBe(title); // the page's keydown listener never fired
  await snap(page, testInfo, 'hud');

  await page.keyboard.press('Escape');
  await expect.poll(async () => (await debugState(ui, tabId)).hudOpen).toBe(false);
});

test('orb: a drag snaps to the nearest edge; follow mode trails the cursor and never blocks it', async ({ context }) => {
  const { page, tabId } = await openFixture(context, { path: 'plain', html: PLAIN });
  const ui = await popup(context);
  await page.bringToFront();
  await expect.poll(async () => (await debugState(ui, tabId)).layout?.popoverOpen).toBe(true);

  const { orb } = (await debugState(ui, tabId)).layout!;
  await page.mouse.move(orb.x + 28, orb.y + 28);
  await page.mouse.down();
  await page.mouse.move(300, 300, { steps: 10 });
  await page.mouse.up();
  await expect.poll(async () => Math.round((await debugState(ui, tabId)).layout!.orb.x), { timeout: 10_000 }).toBe(24);
  expect((await debugState(ui, tabId)).hudOpen).toBe(false); // a drag is not a click

  await ui.evaluate(() => chrome.runtime.sendMessage({ type: 'settings.set', patch: { companionMode: 'follow' } }));
  await expect.poll(async () => (await debugState(ui, tabId)).companionMode).toBe('follow');
  await page.mouse.move(500, 300, { steps: 5 });
  await expect
    .poll(async () => {
      const { x, y } = (await debugState(ui, tabId)).layout!.orb;
      return Math.abs(x - 522) < 1.5 && Math.abs(y - 322) < 1.5;
    }, { timeout: 10_000 })
    .toBe(true);
  expect((await debugState(ui, tabId)).layout!.hitAtOrb).toBe('page');
});
