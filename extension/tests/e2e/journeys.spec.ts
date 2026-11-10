/**
 * The three end-to-end journeys, by voice, on the fake profile (no models, no keys, deterministic).
 *
 * 1. First run: pair on the welcome page, turn on the latency timeline, then hold Right Ctrl and ask - the answer
 *    is spoken, captioned, and points at the element it names.
 * 2. A spoken request for a picture: the brain asks for a scene, codegen writes it, the stage shows it.
 * 3. A spoken request to do something on the site: consent window, the fake worker's steps, its question before
 *    the irreversible step, the result in the tracker.
 *
 * The fake microphone always says "hello from the fake microphone"; the fake brain takes its cue from a marker in
 * the page text ([point], [scene...], [task]), which reaches it inside the page context like any other page text.
 */
import type { BrowserContext, Page } from '@playwright/test';

import { DEV_ID, E2E_PORT, E2E_TOKEN } from '../../playwright.config';
import { debugState, expect, openFixture, pair, popup, test, voiceTurns } from './fixtures';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const chrome: any;

function page(cue: string): string {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Journey</title></head>
<body style="margin:0;font:16px sans-serif"><main style="padding:40px">
<h1>Popover API</h1>
<p><button>Show popover</button> <button>Hide popover</button></p>
<p>The Popover API shows content on top of other page content.</p>
<p style="color:#999">Fake brain cue: ${cue}</p></main></body></html>`;
}

/** Hold Right Ctrl like a person asking one question, and wait for the voice turn to end. */
async function speak(tab: Page, ui: Page): Promise<void> {
  const before = (await voiceTurns(ui)).length;
  await tab.bringToFront();
  await tab.keyboard.down('ControlRight');
  await tab.waitForTimeout(1200);
  await tab.keyboard.up('ControlRight');
  await expect.poll(async () => (await voiceTurns(ui)).length, { timeout: 10_000 }).toBe(before + 1);
  await expect.poll(async () => (await voiceTurns(ui)).at(-1)?.outcome, { timeout: 30_000 }).toBe('end');
  expect((await voiceTurns(ui)).at(-1)!.transcript).toBe('hello from the fake microphone');
}

async function welcomePage(context: BrowserContext): Promise<Page> {
  const open = context.pages().find((p) => p.url().includes('/onboarding.html'));
  return open ?? context.waitForEvent('page', { predicate: (p) => p.url().includes('/onboarding.html') });
}

test('first run: pair on the welcome page, then a spoken question is answered aloud and pointed at',
  async ({ context, sw }) => {
    // Pairing as a new user does it: the welcome page opens by itself on install.
    const welcome = await welcomePage(context);
    await welcome.getByTestId('backend-url').fill(`ws://127.0.0.1:${E2E_PORT}/v1/ws`);
    await welcome.getByTestId('token').fill(E2E_TOKEN);
    await welcome.getByTestId('save').click();
    await expect(welcome.getByTestId('conn-state')).toHaveAttribute('data-state', 'connected', { timeout: 20_000 });
    expect(new URL(sw.url()).host).toBe(DEV_ID);

    // Developer option: the latency timeline under the answer.
    const options = await context.newPage();
    await options.goto(`chrome-extension://${DEV_ID}/options.html`);
    await options.getByTestId('dev-timeline').check();
    await options.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(options.getByText('Saved.')).toBeVisible();

    const { page: tab, tabId } = await openFixture(context, { path: 'journey-point', html: page('[point]') });
    const ui = await popup(context);
    await expect.poll(async () => (await debugState(ui, tabId)).conn).toBe('connected');
    await speak(tab, ui);

    await expect.poll(async () => (await debugState(ui, tabId)).caption, { timeout: 15_000 })
      .toBe('Right over here. The Show popover is what you want.');      // the <point/> tag is neither said nor shown
    await expect.poll(async () => (await debugState(ui, tabId)).lastDirective, { timeout: 10_000 })
      .toEqual({ kind: 'point', target: 'Show popover' });
    await expect.poll(async () => (await debugState(ui, tabId)).annotations, { timeout: 10_000 })
      .toBeGreaterThanOrEqual(1);                                            // the pointer landed (after its flight)
    const record = (await voiceTurns(ui)).at(-1)!;
    expect(record.latencyMs).toBeGreaterThan(0);                           // key release -> audible answer

    await expect.poll(async () => (await debugState(ui, tabId)).timeline?.kind, { timeout: 10_000 }).toBe('voice');
    const { timeline, devTimeline } = await debugState(ui, tabId);
    expect(devTimeline).toBe(true);
    expect(timeline!.marks.map((m) => m.label)).toEqual(expect.arrayContaining(['transcript', 'heard']));
    test.info().annotations.push({ type: 'timeline', description: JSON.stringify(timeline) });
  });

test('a spoken request for a picture: the stage shows the scene codegen wrote', async ({ context, sw }) => {
  await pair(context, sw);
  const { page: tab, tabId } = await openFixture(context, {
    path: 'journey-scene', html: page('[scene:svg|Water cycle]'),
  });
  const ui = await popup(context);
  await expect.poll(async () => (await debugState(ui, tabId)).conn).toBe('connected');
  await speak(tab, ui);

  await expect.poll(async () => (await debugState(ui, tabId)).caption, { timeout: 15_000 })
    .toBe('Here you go. It should appear in a moment.');
  await expect.poll(async () => (await debugState(ui, tabId)).scene?.status, { timeout: 20_000 }).toBe('shown');
  expect((await debugState(ui, tabId)).scene).toMatchObject({ kind: 'svg', title: 'Water cycle', attempt: 1 });
});

test('a spoken request to act on the site: consent, the agent steps, asks first, and reports back',
  async ({ context, sw }) => {
    await pair(context, sw);
    const { page: tab, tabId } = await openFixture(context, {
      path: 'journey-task', html: page('[task]'),
      headers: { 'Set-Cookie': 'session=alice-token; Path=/; HttpOnly; SameSite=Lax' },
    });
    const ui = await popup(context);
    await expect.poll(async () => (await debugState(ui, tabId)).conn).toBe('connected');

    const consentOpened = context.waitForEvent('page', {
      predicate: (p) => p.url().includes('/consent.html'), timeout: 30_000,
    });
    await speak(tab, ui);
    const consent = await consentOpened;
    await expect(consent.getByTestId('consent-goal')).toHaveText('hello from the fake microphone');
    await expect(consent.getByTestId('consent-tab-site')).toContainText('e2e.test');
    await expect(consent.getByTestId('consent-session')).toContainText('cookies (1)');
    await expect(consent.getByTestId('consent-allow')).toBeEnabled();         // armed after 1.5 s
    await consent.getByTestId('consent-allow').click();

    await expect.poll(async () => (await debugState(ui, tabId)).task?.status, { timeout: 20_000 }).toBe('confirm');
    const running = (await debugState(ui, tabId)).task!;
    expect(running.steps.map((s) => s.text)).toEqual(expect.arrayContaining(['Finding the item', 'Adding it to the cart']));
    expect(running.confirm?.question).toBe('Place the order for $12.00?');
    await ui.evaluate((id) => chrome.runtime.sendMessage({ type: 'debug.taskAnswer', tabId: id, approved: true }), tabId);
    await expect.poll(async () => (await debugState(ui, tabId)).task?.status, { timeout: 15_000 }).toBe('done');
    expect((await debugState(ui, tabId)).task?.summary).toBe('Added the item to your cart and placed the order.');
  });
