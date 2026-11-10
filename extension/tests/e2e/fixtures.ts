/** Shared Playwright fixtures: the built extension in bundled Chromium, pairing, fixture pages, debug probes. */
import { fileURLToPath } from 'node:url';

import { type BrowserContext, chromium, expect, type Page, test as base, type Worker } from '@playwright/test';

import type { DebugStateReply, VoiceTurnRecord } from '../../src/lib/messages';
import { DEV_ID, E2E_PORT, E2E_TOKEN } from '../../playwright.config';

// `chrome` exists only inside the extension contexts that the evaluate() callbacks run in.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const chrome: any;

const EXTENSION = fileURLToPath(new URL('../../.output/chrome-mv3', import.meta.url));
export const FIXTURE = 'http://e2e.test/';

export interface Backend {
  port: number;
  token: string;
}

export const test = base.extend<{ dpr: number; micFile: string | null; backend: Backend; browserArgs: string[];
  context: BrowserContext; sw: Worker }>({
  /** devicePixelRatio of the browser (`test.use({ dpr: 2 })`): coordinate tests run at 1, 1.25 and 2. */
  dpr: [1, { option: true }],
  /** A WAV the fake microphone plays (looped); null = Chromium's built-in beep. */
  micFile: [null, { option: true }],
  /** Which backend `pair()` connects to (default: the fake-profile webServer). */
  backend: [{ port: E2E_PORT, token: E2E_TOKEN }, { option: true }],
  /** Extra Chromium flags (e.g. --host-resolver-rules for a local test site). */
  browserArgs: [[], { option: true }],
  context: async ({ dpr, micFile, browserArgs }, use) => {
    const context = await chromium.launchPersistentContext('', {
      channel: 'chromium',
      viewport: { width: 1280, height: 800 },
      deviceScaleFactor: dpr,
      args: [
        `--disable-extensions-except=${EXTENSION}`,
        `--load-extension=${EXTENSION}`,
        // A microphone without a permission prompt (the offscreen recorder) and audio without a user gesture.
        '--use-fake-ui-for-media-stream',
        '--use-fake-device-for-media-stream',
        ...(micFile ? [`--use-file-for-fake-audio-capture=${micFile}`] : []),
        '--autoplay-policy=no-user-gesture-required',
        ...browserArgs,
      ],
    });
    await use(context);
    await context.close();
  },
  sw: async ({ context }, use) => {
    const sw = context.serviceWorkers()[0] ?? (await context.waitForEvent('serviceworker'));
    expect(new URL(sw.url()).host).toBe(DEV_ID); // the pinned key gives the expected extension ID
    await use(sw);
  },
});

export { expect };

export async function connState(sw: Worker): Promise<string | undefined> {
  return sw.evaluate(async () => {
    const stored = await chrome.storage.session.get('conn');
    return (stored.conn as { state?: string } | undefined)?.state;
  });
}

export async function pair(context: BrowserContext, sw: Worker, backend: Backend = { port: E2E_PORT, token: E2E_TOKEN }):
  Promise<void> {
  for (const page of context.pages()) {
    if (page.url().includes('/onboarding.html')) await page.close(); // opened on install while unpaired
  }
  await sw.evaluate(
    ({ url, token }) => chrome.storage.local.set({ backendUrl: url, token }),
    { url: `ws://127.0.0.1:${backend.port}/v1/ws`, token: backend.token },
  );
  await expect.poll(() => connState(sw)).toBe('connected');
}

export interface FixtureOptions {
  path?: string;
  html?: string;
  headers?: Record<string, string>;
}

/** Serves `html` at http://e2e.test/<path> (with optional response headers) and opens it in a new tab. */
export async function openFixture(
  context: BrowserContext,
  { path = '', html = '<!doctype html><title>fixture</title><h1>Hello page</h1>', headers = {} }: FixtureOptions = {},
): Promise<{ page: Page; tabId: number; sw: Worker }> {
  const page = await context.newPage();
  const url = `${FIXTURE}${path}`;
  await page.route(url, (route) => route.fulfill({ contentType: 'text/html', headers, body: html }));
  await page.goto(url);
  const sw = context.serviceWorkers()[0]!;
  const tabId = await sw.evaluate(async (target) => (await chrome.tabs.query({ url: target }))[0]?.id, url);
  expect(tabId).toBeDefined();
  return { page, tabId: tabId!, sw };
}

export async function popup(context: BrowserContext): Promise<Page> {
  const page = await context.newPage();
  await page.goto(`chrome-extension://${DEV_ID}/popup.html`);
  return page;
}

/** The worker's record of recent push-to-talk turns (timings, transcript, outcome, backend metrics). */
export async function voiceTurns(ui: Page): Promise<VoiceTurnRecord[]> {
  const reply = await ui.evaluate(() => chrome.runtime.sendMessage({ type: 'debug.voice' }));
  return (reply as { turns: VoiceTurnRecord[] }).turns;
}

/** Hold the push-to-talk chord (Right Ctrl) for `ms` on `page`. */
export async function holdToTalk(page: Page, ms: number): Promise<void> {
  await page.keyboard.down('ControlRight');
  await page.waitForTimeout(ms);
  await page.keyboard.up('ControlRight');
}

/** The content script's debug.state (the shadow root is closed, so this is the only way to look inside). */
export async function debugState(ui: Page, tabId: number): Promise<DebugStateReply> {
  return ui.evaluate((id) => chrome.runtime.sendMessage({ type: 'debug.state', tabId: id }), tabId);
}
