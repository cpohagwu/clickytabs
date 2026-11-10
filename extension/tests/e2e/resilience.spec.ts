/**
 * The extension recovers when Chrome stops its service worker or closes its offscreen document in the middle of
 * a turn (the microphone is released, the tab gets a message instead of thinking forever, the next turn works), and
 * answers stay in the tab that asked while several tabs share the backend connection.
 */
import type { BrowserContext, Page } from '@playwright/test';

import { debugState, expect, openFixture, pair, popup, test, voiceTurns } from './fixtures';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const chrome: any;

const PAGE = '<!doctype html><title>resilience</title><body style="margin:0"><h1>Resilience fixture</h1>'
  + '<p>Some text to talk about.</p></body>';

async function openHud(page: Page, ui: Page, tabId: number): Promise<void> {
  await page.bringToFront();
  if ((await debugState(ui, tabId)).hudOpen) return;
  const { orb } = (await debugState(ui, tabId)).layout!;
  await page.mouse.click(orb.x + orb.width / 2, orb.y + orb.height / 2);
  await expect.poll(async () => (await debugState(ui, tabId)).hudOpen).toBe(true);
}

async function hudAsk(page: Page, ui: Page, tabId: number, text: string): Promise<void> {
  await openHud(page, ui, tabId);
  await page.keyboard.type(text);
  await page.keyboard.press('Enter');
}

/**
 * Stops the extension's service worker the way Chrome does (DevTools' "Stop" uses the same call) and waits until it
 * has stopped. Playwright keeps reporting the old Worker object, so state is read through extension pages afterwards.
 */
async function stopWorker(context: BrowserContext, ui: Page): Promise<void> {
  const cdp = await context.newCDPSession(ui);
  let stopped = false;
  cdp.on('ServiceWorker.workerVersionUpdated', (event: { versions: { runningStatus: string }[] }) => {
    if (event.versions.some((v) => v.runningStatus === 'stopped')) stopped = true;
  });
  await cdp.send('ServiceWorker.enable');
  await cdp.send('ServiceWorker.stopAllWorkers');
  await expect.poll(() => stopped, { timeout: 10_000 }).toBe(true);
  await cdp.detach();
}

/** The connection state the (current) worker published, read from an extension page. */
async function connFromPage(ui: Page): Promise<string | undefined> {
  return ui.evaluate(async () => (await chrome.storage.session.get('conn')).conn?.state);
}

async function offscreenRecording(ui: Page): Promise<boolean | undefined> {
  const pong = await ui.evaluate(() => chrome.runtime.sendMessage({ type: 'debug.offscreenPing' }));
  return pong?.ok ? pong.recording : undefined;
}

test('a turn in flight when the service worker stops ends with a message, and the next question works',
  async ({ context, sw }) => {
    await pair(context, sw);
    const { page, tabId } = await openFixture(context, { path: 'sw-stop', html: PAGE });
    const ui = await popup(context);
    await hudAsk(page, ui, tabId, '[slow] tell me a long story');
    await expect.poll(async () => (await debugState(ui, tabId)).caption, { timeout: 10_000 }).not.toBe('');
    expect((await debugState(ui, tabId)).busy).toBe(true);

    await stopWorker(context, ui);
    // Nothing else is open to wake it (no offscreen document yet): the next message from a page does, as the 30 s
    // watchdog alarm would.
    await expect.poll(async () => ui.evaluate(() => chrome.runtime.sendMessage({ type: 'status.get' })
      .then(() => true, () => false)), { timeout: 15_000 }).toBe(true);
    await expect.poll(async () => (await debugState(ui, tabId)).error, { timeout: 15_000 })
      .toContain('connection to the backend was interrupted');
    expect((await debugState(ui, tabId)).busy).toBe(false);
    await expect.poll(() => connFromPage(ui), { timeout: 20_000 }).toBe('connected');

    await hudAsk(page, ui, tabId, 'are you back?');
    await expect.poll(async () => (await debugState(ui, tabId)).caption, { timeout: 15_000 })
      .toContain('are you back?');
    await expect.poll(async () => (await debugState(ui, tabId)).busy, { timeout: 15_000 }).toBe(false);
  });

test('stopping the service worker while recording releases the microphone, and push-to-talk works again',
  async ({ context, sw }) => {
    await pair(context, sw);
    const { page, tabId } = await openFixture(context, { path: 'sw-rec', html: PAGE });
    const ui = await popup(context);
    await page.bringToFront();
    await page.keyboard.down('ControlRight');
    await expect.poll(() => offscreenRecording(ui), { timeout: 10_000 }).toBe(true);

    await stopWorker(context, ui);
    // The offscreen document's lifeline drops: it lets go of the mic and reconnects, which starts a new worker that
    // reconnects to the backend - before the test sends the worker anything.
    await expect.poll(() => connFromPage(ui), { timeout: 20_000 }).toBe('connected');
    await expect.poll(() => offscreenRecording(ui), { timeout: 10_000 }).toBe(false);
    await page.keyboard.up('ControlRight');
    await expect.poll(async () => (await debugState(ui, tabId)).orbState, { timeout: 10_000 }).not.toBe('listening');

    await page.bringToFront();
    await page.keyboard.down('ControlRight');
    await page.waitForTimeout(1200);
    await page.keyboard.up('ControlRight');
    await expect.poll(async () => (await voiceTurns(ui)).at(-1)?.outcome, { timeout: 20_000 }).toBe('end');
    expect((await voiceTurns(ui)).at(-1)!.transcript).toBe('hello from the fake microphone');
  });

test('closing the offscreen document mid-recording ends the turn at once, and the next push-to-talk works',
  async ({ context, sw }) => {
    await pair(context, sw);
    const { page, tabId } = await openFixture(context, { path: 'offscreen-close', html: PAGE });
    const ui = await popup(context);
    await page.bringToFront();
    await page.keyboard.down('ControlRight');
    await expect.poll(() => offscreenRecording(ui), { timeout: 10_000 }).toBe(true);

    await sw.evaluate(() => chrome.offscreen.closeDocument());
    await expect.poll(async () => (await voiceTurns(ui)).at(-1)?.outcome, { timeout: 5_000 }).toBe('aborted');
    expect((await voiceTurns(ui)).at(-1)!.error).toContain('microphone stopped unexpectedly');
    await expect.poll(async () => (await debugState(ui, tabId)).error, { timeout: 5_000 })
      .toContain('microphone stopped unexpectedly');
    await page.keyboard.up('ControlRight');

    await page.waitForTimeout(300);
    await page.keyboard.down('ControlRight');
    await page.waitForTimeout(1200);
    await page.keyboard.up('ControlRight');
    await expect.poll(async () => (await voiceTurns(ui)).at(-1)?.outcome, { timeout: 20_000 }).toBe('end');
  });

test('with two tabs talking, each answer stays in the tab that asked', async ({ context, sw }) => {
  await pair(context, sw);
  const a = await openFixture(context, { path: 'tab-a', html: PAGE });
  const b = await openFixture(context, { path: 'tab-b', html: PAGE });
  const ui = await popup(context);

  await hudAsk(a.page, ui, a.tabId, '[slow] question from tab A');
  await expect.poll(async () => (await debugState(ui, a.tabId)).caption, { timeout: 10_000 }).not.toBe('');
  await hudAsk(b.page, ui, b.tabId, 'question from tab B');       // one conversation: B's question replaces A's

  await expect.poll(async () => (await debugState(ui, b.tabId)).caption, { timeout: 15_000 })
    .toContain('question from tab B');
  await expect.poll(async () => (await debugState(ui, b.tabId)).busy, { timeout: 15_000 }).toBe(false);
  await expect.poll(async () => (await debugState(ui, a.tabId)).busy, { timeout: 15_000 }).toBe(false);
  const captionA = (await debugState(ui, a.tabId)).caption;
  expect(captionA).not.toContain('question from tab B');
  expect((await debugState(ui, b.tabId)).caption).not.toContain('question from tab A');
  expect((await debugState(ui, a.tabId)).orbState).not.toBe('thinking');
});

test('the options page switches the voice without a restart, and shows a profile fixed by the backend',
  async ({ context, sw }) => {
    await pair(context, sw);
    const options = await context.newPage();
    await options.goto(`chrome-extension://${new URL(sw.url()).host}/options.html`);
    await expect(options.getByTestId('models')).toBeVisible();
    await expect(options.getByTestId('profile-select')).toBeDisabled();      // CLICKY_PROFILE=fake in the e2e backend
    await expect(options.getByTestId('cap-brain')).toBeVisible();
    await expect(options.getByTestId('status-tts')).toContainText('fake');

    await options.getByTestId('voice-tts').selectOption('fake-calm');
    await options.getByTestId('apply-tts').click();
    await expect(options.getByTestId('config-note')).toBeVisible({ timeout: 20_000 });
    await expect(options.getByTestId('reset-tts')).toBeVisible();

    await options.reload();                                                  // saved on the backend
    await expect(options.getByTestId('voice-tts')).toHaveValue('fake-calm', { timeout: 15_000 });
    await options.getByTestId('reset-tts').click();
    await expect(options.getByTestId('reset-tts')).toHaveCount(0, { timeout: 20_000 });
    await expect(options.getByTestId('voice-tts')).toHaveValue('');
  });
