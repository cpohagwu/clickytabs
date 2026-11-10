/**
 * E2E: scenes.
 *
 * - The sandbox (debug.sceneRender renders straight into the stage, no backend): every kind renders; runtime errors
 *   come back with the failing line; endless loops are stopped; the network is closed; forged "ready" messages from
 *   scene code are ignored; svg/html markup is sanitized.
 * - The whole pipeline on the fake profile: the fake brain asks for a scene, the fake codegen writes it, the stage
 *   shows it; with [broken] the first code fails and the automatic repair turn fixes it.
 */
import type { Page } from '@playwright/test';

import type { RenderResult } from '../../src/lib/scene/types';
import { E2E_PORT } from '../../playwright.config';
import { debugState, expect, openFixture, pair, popup, test } from './fixtures';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const chrome: any;

const PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Scene fixture</title></head>
<body style="margin:0;font:16px sans-serif"><h1 style="margin:40px">Scenes</h1><p>Ask for a scene.</p></body></html>`;

const CUBE = `const cube = new THREE.Mesh(new THREE.BoxGeometry(2, 2, 2), new THREE.MeshStandardMaterial({ color: 0x7c5cff }));
scene.add(cube);
addLabel('Cube', [0, 1.8, 0]);
onFrame((dt) => { cube.rotation.y += dt; });`;

async function render(ui: Page, tabId: number, kind: string, code: string): Promise<RenderResult> {
  return ui.evaluate(
    ({ id, k, c }) => chrome.runtime.sendMessage({ type: 'debug.sceneRender', tabId: id, kind: k, code: c }),
    { id: tabId, k: kind, c: code },
  ) as Promise<RenderResult>;
}

test.describe('scene sandbox', () => {
  test('three, canvas, svg and html scenes render on the stage', async ({ context }) => {
    const { page, tabId } = await openFixture(context, { path: 'm5', html: PAGE });
    const ui = await popup(context);
    await page.bringToFront();

    const three = await render(ui, tabId, 'three', CUBE);
    expect(three.ok, three.error).toBe(true);
    expect(three.stats?.objects).toBeGreaterThanOrEqual(1);
    expect(three.stats?.paintedRatio).toBeGreaterThan(0.01);
    const state = await debugState(ui, tabId);
    expect(state.scene).toMatchObject({ open: true, kind: 'three', status: 'shown' });

    const canvas = await render(ui, tabId, 'canvas', `onFrame((dt, t) => {
      g.clearRect(0, 0, width, height);
      g.fillStyle = '#00f0ff';
      g.fillRect(40 + Math.sin(t) * 20, 40, 120, 80);
    });`);
    expect(canvas.ok, canvas.error).toBe(true);

    const svg = await render(ui, tabId, 'svg', '<svg viewBox="0 0 200 100"><circle cx="50" cy="50" r="40" fill="#ff4d6d"/></svg>');
    expect(svg.ok, svg.error).toBe(true);

    const html = await render(ui, tabId, 'html', '<style>.c{padding:12px;background:#223}</style><div class="c"><h2>HTTP/3</h2><p>QUIC</p></div>');
    expect(html.ok, html.error).toBe(true);
  });

  test('errors come back with the failing line; blank and frozen scenes fail', async ({ context }) => {
    const { page, tabId } = await openFixture(context, { path: 'm5-errors', html: PAGE });
    const ui = await popup(context);
    await page.bringToFront();

    const broken = await render(ui, tabId, 'three', 'const cube = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1));\ncube.spinForever();');
    expect(broken.ok).toBe(false);
    expect(broken.error).toContain('TypeError');
    expect(broken.error).toContain('line 2: cube.spinForever();');

    const syntax = await render(ui, tabId, 'canvas', 'onFrame(() => {');
    expect(syntax.ok).toBe(false);
    expect(syntax.error).toContain('SyntaxError');

    const blank = await render(ui, tabId, 'three', 'const nothing = 1;');
    expect(blank.ok).toBe(false);
    expect(blank.error).toContain('blank scene');

    const offscreen = await render(ui, tabId, 'three',
      'const m = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshStandardMaterial());\nm.position.set(0, 0, 500);\nscene.add(m);');
    expect(offscreen.ok).toBe(false);
    expect(offscreen.error).toContain('outside the camera view');

    const started = Date.now();
    const frozen = await render(ui, tabId, 'canvas', 'onFrame(() => { while (true) {} });');
    expect(frozen.ok).toBe(false);
    expect(frozen.error).toMatch(/stopped responding/);
    expect(Date.now() - started).toBeLessThan(12_000);
    expect((await debugState(ui, tabId)).scene?.status).toBe('failed');
  });

  test('web-page style code (own scene, camera, renderer, canvas, entry function) still renders', async ({ context }) => {
    const { page, tabId } = await openFixture(context, { path: 'm5-compat', html: PAGE });
    const ui = await popup(context);
    await page.bringToFront();

    const boilerplate = await render(ui, tabId, 'three', `const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.1, 1000);
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(window.innerWidth, window.innerHeight);
document.body.appendChild(renderer.domElement);
const cube = new THREE.Mesh(new THREE.BoxGeometry(2, 2, 2), new THREE.MeshStandardMaterial({ color: 0x00ff88 }));
scene.add(cube);
camera.position.z = 5;
function animate() {
  requestAnimationFrame(animate);
  cube.rotation.x += 0.01;
  renderer.render(scene, camera);
}
animate();`);
    expect(boilerplate.ok, boilerplate.error).toBe(true);

    const canvasPage = await render(ui, tabId, 'canvas', `const canvas = document.createElement('canvas');
const ctx = canvas.getContext('2d');
document.body.appendChild(canvas);
async function run() {
  onFrame(() => { ctx.fillStyle = '#7cf0ff'; ctx.fillRect(20, 20, 120, 80); });
}`);
    expect(canvasPage.ok, canvasPage.error).toBe(true);

    const proxy = await render(ui, tabId, 'canvas', `const g = canvas.getContext('2d');
onFrame(() => { ctx.fillStyle = '#ff4d6d'; ctx.fillRect(0, 0, width / 2, height / 2); });`);
    expect(proxy.ok, proxy.error).toBe(true);

    const addon = await render(ui, tabId, 'three', 'const t = new THREE.TextGeometry("Q1");');
    expect(addon.error).toContain('use addLabel(text, position) for text');
  });

  test('scene code has no network and cannot forge its own success', async ({ context }) => {
    const { page, tabId } = await openFixture(context, { path: 'm5-net', html: PAGE });
    const ui = await popup(context);
    await page.bringToFront();

    // A no-cors fetch to a reachable server would succeed (opaque response) unless the CSP blocks it.
    const network = await render(ui, tabId, 'three', `let outcome = 'blocked';
      try { await fetch('http://127.0.0.1:${E2E_PORT}/healthz', { mode: 'no-cors' }); outcome = 'reached'; } catch {}
      throw new Error('network ' + outcome);`);
    expect(network.ok).toBe(false);
    expect(network.error).toContain('network blocked');

    // The runtime's messages carry a token the scene code never sees: a forged "ready" is ignored.
    const forged = await render(ui, tabId, 'canvas', `self.postMessage({ type: 'ready', token: 'guess', stats: {} });
      postMessage({ type: 'ready', stats: {} });
      onFrame(() => { while (true) {} });`);
    expect(forged.ok).toBe(false);
  });

  test('svg and html markup is sanitized before it is shown', async ({ context }) => {
    const { page, tabId } = await openFixture(context, { path: 'm5-sanitize', html: PAGE });
    const ui = await popup(context);
    await page.bringToFront();

    const html = await render(ui, tabId, 'html', `<meta http-equiv="refresh" content="0;url=https://evil.example">
      <base href="https://evil.example/"><script>parent.postMessage('owned', '*')</script>
      <div onclick="alert(1)"><a href="https://evil.example">Visit</a><img src="x" onerror="alert(1)">
      <form action="https://evil.example"><input name="q"></form><iframe src="https://evil.example"></iframe>
      <p>Safe text</p></div>`);
    expect(html.ok, html.error).toBe(true);
    expect(html.stats?.removed).toEqual(expect.arrayContaining(['<meta>', '<base>', '<script>', 'onclick', '<a>',
      '<form>', '<iframe>']));

    const svg = await render(ui, tabId, 'svg', `<svg viewBox="0 0 100 100" onload="alert(1)">
      <a href="javascript:alert(1)"><rect width="50" height="50" fill="red"/></a>
      <foreignObject><div>html</div></foreignObject>
      <set attributeName="href" to="javascript:alert(1)"/><use href="https://evil.example/x.svg#a"/>
      <circle cx="70" cy="70" r="20" fill="#0ff"/></svg>`);
    expect(svg.ok, svg.error).toBe(true);
    expect(svg.stats?.removed).toEqual(expect.arrayContaining(['onload', '<a>', '<foreignobject>', 'attributename']));
    // Still on the fixture page and the stage still shows the scene: nothing navigated.
    expect(page.url()).toContain('m5-sanitize');
    expect((await debugState(ui, tabId)).scene?.status).toBe('shown');
  });
});

test.describe('scene pipeline (fake profile)', () => {
  test('the brain asks for a scene, codegen writes it, the stage shows it', async ({ context, sw }) => {
    await pair(context, sw);
    const { page, tabId } = await openFixture(context, { path: 'm5-pipeline', html: PAGE });
    const ui = await popup(context);
    await page.bringToFront();
    await expect.poll(async () => (await debugState(ui, tabId)).conn).toBe('connected');
    const { orb } = (await debugState(ui, tabId)).layout!;
    await page.mouse.click(orb.x + orb.width / 2, orb.y + orb.height / 2);
    await expect.poll(async () => (await debugState(ui, tabId)).hudOpen).toBe(true);

    await page.keyboard.type('[scene] show me a cube');
    await page.keyboard.press('Enter');
    await expect.poll(async () => (await debugState(ui, tabId)).caption)
      .toBe('Here you go. It should appear in a moment.');                 // the <scene> brief is never shown
    await expect.poll(async () => (await debugState(ui, tabId)).scene?.status, { timeout: 15_000 }).toBe('shown');
    expect((await debugState(ui, tabId)).scene).toMatchObject({ kind: 'three', title: 'Fake three scene', attempt: 1 });

    // A failing scene gets one automatic repair turn.
    await page.keyboard.type('[scene:svg] [broken] draw a diagram');
    await page.keyboard.press('Enter');
    await expect.poll(async () => (await debugState(ui, tabId)).scene?.title, { timeout: 15_000 }).toBe('Fake svg scene');
    await expect.poll(async () => (await debugState(ui, tabId)).scene?.status, { timeout: 15_000 }).toBe('shown');
    expect((await debugState(ui, tabId)).scene?.attempt).toBe(2);
  });
});
