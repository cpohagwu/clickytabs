/**
 * E2E: page context. Element boxes are checked against the pixels of the real screenshot at DPR 1, 1.25 and 2
 * (with the page scrolled), the payload is checked for leaked input values, the companion must stay out of the
 * capture, and a HUD turn must deliver the page to the backend (the fake brain describes what it received).
 */
import { writeFileSync } from 'node:fs';

import type { Page } from '@playwright/test';

import type { PageContextReply } from '../../src/lib/messages';
import type { PageContext } from '../../src/lib/protocol.gen';
import { debugState, expect, openFixture, pair, popup, test } from './fixtures';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const chrome: any;

const FIXTURE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Page-context fixture</title>
<style>
  body { margin: 0; background: #fff; font: 16px sans-serif; }
  .abs { position: absolute; }
  #red { left: 100px; top: 120px; width: 160px; height: 60px; background: #f00; color: #f00; border: 0; }
  #blue { left: 700px; top: 400px; width: 200px; height: 80px; background: #00f; color: #00f; display: block; }
</style></head>
<body>
  <h1 class="abs" style="left: 20px; top: 60px; margin: 0">Page-context fixture</h1>
  <button id="red" class="abs">Red button</button>
  <a id="blue" class="abs" href="#b">Blue link</a>
  <label class="abs" for="email" style="left: 100px; top: 300px">Email</label>
  <input class="abs" id="email" style="left: 160px; top: 300px" value="private@example.com">
  <label class="abs" for="pw" style="left: 100px; top: 340px">Password</label>
  <input class="abs" id="pw" type="password" style="left: 190px; top: 340px" value="hunter2">
  <textarea class="abs" style="left: 100px; top: 380px">draft secret</textarea>
  <p class="abs" style="left: 400px; top: 200px">Some visible paragraph text for the excerpt.</p>
  <div style="height: 3000px"></div>
</body></html>`;

const SCROLL_Y = 50;

async function pageContext(ui: Page, tabId: number): Promise<Extract<PageContextReply, { ok: true }>> {
  const reply: PageContextReply = await ui.evaluate(
    (id) => chrome.runtime.sendMessage({ type: 'debug.pageContext', tabId: id }), tabId);
  if (!reply.ok) throw new Error(reply.error);
  // Every caller needs the picture; when it is missing, say why (capture error, stale frames) instead of a null deref.
  expect(reply.page.screenshot, `no screenshot: ${JSON.stringify(reply.capture)}`).not.toBeNull();
  return reply;
}

/** Decodes the screenshot in an extension page and reads pixels at the given image coordinates. */
async function pixels(ui: Page, context: PageContext, points: [number, number][]): Promise<number[][]> {
  return ui.evaluate(async ({ b64, at }) => {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/jpeg' }));
    const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
    const ctx = canvas.getContext('2d')!;
    ctx.drawImage(bitmap, 0, 0);
    return at.map(([x, y]) => [...ctx.getImageData(Math.round(x), Math.round(y), 1, 1).data.slice(0, 3)]);
  }, { b64: context.screenshot!.data_b64, at: points });
}

/** Share of screenshot pixels that are companion neon (saturated, strong blue channel). */
async function neonShare(ui: Page, context: PageContext): Promise<number> {
  return ui.evaluate(async (b64) => {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/jpeg' }));
    const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
    const ctx = canvas.getContext('2d')!;
    ctx.drawImage(bitmap, 0, 0);
    const { data } = ctx.getImageData(0, 0, bitmap.width, bitmap.height);
    let neon = 0;
    for (let i = 0; i < data.length; i += 4) {
      const [r, g, b] = [data[i]!, data[i + 1]!, data[i + 2]!];
      const max = Math.max(r, g, b);
      // magenta/cyan/violet, but not the fixture's pure blue (#00f has g = r = 0)
      if (max > 0 && (max - Math.min(r, g, b)) / max > 0.45 && b > 140 && g > 40 && b >= g * 0.85) neon += 1;
    }
    return neon / (data.length / 4);
  }, context.screenshot!.data_b64);
}

async function openM2(context: Parameters<typeof openFixture>[0]) {
  const opened = await openFixture(context, { path: 'm2', html: FIXTURE });
  const ui = await popup(context);
  await opened.page.bringToFront(); // captureVisibleTab sees the active tab only
  await expect.poll(async () => (await debugState(ui, opened.tabId)).layout?.popoverOpen).toBe(true);
  return { ...opened, ui };
}

for (const dpr of [1, 1.25, 2]) {
  test.describe(`DPR ${dpr}`, () => {
    test.use({ dpr });

    test(`element boxes land on the right screenshot pixels (DPR ${dpr}, scrolled)`, async ({ context }) => {
      const { page, tabId, ui } = await openM2(context);
      await page.evaluate((y) => window.scrollTo(0, y), SCROLL_Y);
      const reply = await pageContext(ui, tabId);
      const ctx = reply.page;
      writeFileSync(test.info().outputPath('screenshot.jpg'), Buffer.from(ctx.screenshot!.data_b64, 'base64'));

      expect(ctx.viewport).toMatchObject({ width: 1280, height: 800, scroll_y: SCROLL_Y });
      expect(ctx.viewport.dpr).toBeCloseTo(dpr, 2);
      expect(ctx.screenshot).toMatchObject({ width: 1024, height: 640, mime: 'image/jpeg' }); // compact budget
      const factor = 1024 / 1280;
      const elements = ctx.elements ?? [];
      const red = elements.find((e) => e.name === 'Red button')!;
      const blue = elements.find((e) => e.name === 'Blue link')!;
      const expected = [100, 120 - SCROLL_Y, 160, 60].map((v) => v * factor);
      red.bbox.forEach((v, i) => expect(Math.abs(v - expected[i]!)).toBeLessThanOrEqual(1));

      const center = (b: [number, number, number, number]): [number, number] => [b[0] + b[2] / 2, b[1] + b[3] / 2];
      const [redPx, bluePx, cornerPx] = await pixels(ui, ctx, [center(red.bbox), center(blue.bbox), [1000, 610]]);
      expect(redPx![0]).toBeGreaterThan(200);
      expect(redPx![1]! + redPx![2]!).toBeLessThan(90);
      expect(bluePx![2]).toBeGreaterThan(200);
      expect(bluePx![0]! + bluePx![1]!).toBeLessThan(90);
      expect(Math.min(...cornerPx!)).toBeGreaterThan(235); // where the orb sits: hidden during capture, page white

      expect(reply.snapshotMs).toBeLessThan(30);
      test.info().annotations.push({ type: 'capture', description: JSON.stringify(reply.capture) });
      test.info().annotations.push({ type: 'timing', description: `snapshot ${reply.snapshotMs} ms, total ${reply.totalMs} ms` });
    });
  });
}

test('never sends input values (not in the DOM payload, masked in the screenshot)', async ({ context }, testInfo) => {
  const { tabId, ui } = await openM2(context);
  const ctx = (await pageContext(ui, tabId)).page;
  writeFileSync(testInfo.outputPath('masked.jpg'), Buffer.from(ctx.screenshot!.data_b64, 'base64'));
  const all = JSON.stringify(ctx);
  for (const secret of ['private@example.com', 'hunter2', 'draft secret']) expect(all).not.toContain(secret);
  const fields = (ctx.elements ?? []).filter((e) => e.tag === 'input:text' || e.tag === 'textarea');
  expect(fields).toHaveLength(2);
  // Sample along each field's middle row: every pixel is the flat mask (no glyphs of the typed text).
  for (const { bbox: [x, y, w, h] } of fields) {
    const row = await pixels(ui, ctx, Array.from({ length: 12 }, (_, i) => [x + 3 + ((w - 6) * i) / 11, y + h / 2]));
    for (const px of row) expect(Math.min(...px)).toBeGreaterThan(205);
  }
  expect(ctx.elements).toContainEqual(expect.objectContaining({ role: 'textbox', name: 'Email', tag: 'input:text' }));
  expect(ctx.elements).toContainEqual(expect.objectContaining({ role: 'textbox', name: 'Password', tag: 'input:password' }));
  expect(ctx.elements).toContainEqual(expect.objectContaining({ role: 'heading', name: 'Page-context fixture', level: 1 }));
  expect(ctx.text_excerpt).toContain('Some visible paragraph text for the excerpt.');
  expect(ctx.url).toBe('http://e2e.test/m2');
});

test('the companion and its drawings stay out of the screenshot, then come back', async ({ context }, testInfo) => {
  const { page, tabId, ui } = await openM2(context);
  expect(await ui.evaluate((id) => chrome.runtime.sendMessage({ type: 'demo.run', tabId: id }), tabId)).toEqual({ ok: true });
  await expect.poll(async () => (await debugState(ui, tabId)).annotations, { timeout: 15_000 }).toBeGreaterThanOrEqual(2);
  await page.screenshot({ path: testInfo.outputPath('with-annotations.png') });
  const reply = await pageContext(ui, tabId);
  testInfo.annotations.push({ type: 'capture', description: JSON.stringify(reply.capture) });
  expect(reply.capture.error).toBeUndefined();
  expect(reply.page.screenshot).not.toBeNull();
  const ctx = reply.page;
  expect(await neonShare(ui, ctx)).toBeLessThan(0.0005);
  // Visible again right after the capture.
  const { layout } = await debugState(ui, tabId);
  const shot = await page.screenshot({ clip: layout!.orb });
  const painted = await ui.evaluate(async (b64) => {
    const bitmap = await createImageBitmap(new Blob([Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))]));
    const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
    const c = canvas.getContext('2d')!;
    c.drawImage(bitmap, 0, 0);
    const { data } = c.getImageData(0, 0, bitmap.width, bitmap.height);
    let count = 0;
    for (let i = 0; i < data.length; i += 4) if (Math.min(data[i]!, data[i + 1]!, data[i + 2]!) < 200) count += 1;
    return count / (data.length / 4);
  }, shot.toString('base64'));
  expect(painted).toBeGreaterThan(0.3);
});

test('a HUD question carries the page to the backend', async ({ context, sw }) => {
  await pair(context, sw);
  const { page, tabId, ui } = await openM2(context);
  await expect.poll(async () => (await debugState(ui, tabId)).conn).toBe('connected');
  const { orb } = (await debugState(ui, tabId)).layout!;
  await page.mouse.click(orb.x + orb.width / 2, orb.y + orb.height / 2);
  await expect.poll(async () => (await debugState(ui, tabId)).hudOpen).toBe(true);
  await page.keyboard.type('[page] what is on this page?');
  await page.keyboard.press('Enter');
  await expect
    .poll(async () => (await debugState(ui, tabId)).caption)
    .toMatch(/^Page 'Page-context fixture' with \d+ elements and 1 screenshot\(s\) 1024x640\.$/);
  expect((await debugState(ui, tabId)).hudOpen).toBe(true); // the capture didn't close or blur the HUD
});

test('"Show what the brain sees" outlines every mark with its id', async ({ context }, testInfo) => {
  const { page, tabId, ui } = await openM2(context);
  const ctx = (await pageContext(ui, tabId)).page;
  writeFileSync(testInfo.outputPath('brain-view.jpg'), Buffer.from(ctx.screenshot!.data_b64, 'base64'));
  expect(await ui.evaluate((id) => chrome.runtime.sendMessage({ type: 'debug.marks', tabId: id }), tabId)).toEqual({ ok: true });
  await expect.poll(async () => (await debugState(ui, tabId)).annotations).toBe(ctx.elements?.length);
  await page.waitForTimeout(700);
  await page.screenshot({ path: testInfo.outputPath('marks.png') });
});
