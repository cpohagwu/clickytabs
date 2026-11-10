/**
 * Opt-in acceptance: a spoken question, answered by a real brain, points at the element it names.
 *
 *   # a backend on the profile to check (local or cloud), with a throw-away token - see voice-latency.spec.ts
 *   $env:CC_E2E_POINTING_WAV = 'C:\path\where-popover.wav'    # 16-bit mono WAV: "Where is the show popover button?"
 *   $env:CC_E2E_VOICE_BACKEND = '18766:latency-token-0123456789'
 *   npx playwright test voice-pointing
 *
 * Up to CC_E2E_POINTING_TURNS (default 3) push-to-talk turns; each must end with the pointer on "Show popover".
 */
import { debugState, expect, holdToTalk, openFixture, pair, popup, test, voiceTurns } from './fixtures';

const TURNS = Number(process.env.CC_E2E_POINTING_TURNS ?? 3);
const [port, token] = (process.env.CC_E2E_VOICE_BACKEND ?? '').split(':');

const PAGE = `<!doctype html><html lang="en"><head><title>Popover API - Web APIs</title></head>
<body style="margin:0;font:16px sans-serif">
<header><nav><a href="#">Docs</a> <a href="#">Guides</a> <a href="#">Reference</a></nav></header>
<main><h1>Popover API</h1>
<p>The Popover API provides developers with a standard, consistent, flexible mechanism for displaying popover content
on top of other page content.</p>
<p><button>Copy link</button> <button>Show popover</button> <button>Hide popover</button></p></main></body></html>`;

test.describe('voice pointing', () => {
  test.skip(!process.env.CC_E2E_POINTING_WAV || !port, 'set CC_E2E_POINTING_WAV and CC_E2E_VOICE_BACKEND');
  test.use({ micFile: process.env.CC_E2E_POINTING_WAV ?? null });
  test.setTimeout(5 * 60_000);

  test('a spoken question points at the element it names', async ({ context, sw }) => {
    await pair(context, sw, { port: Number(port), token: token! });
    const { page, tabId } = await openFixture(context, { path: 'pointing', html: PAGE });
    const ui = await popup(context);
    await page.bringToFront();
    await expect.poll(async () => (await debugState(ui, tabId)).conn).toBe('connected');

    for (let turn = 1; turn <= TURNS; turn += 1) {
      await holdToTalk(page, 3_000);
      await expect.poll(async () => (await voiceTurns(ui)).at(-1)?.outcome, { timeout: 120_000 }).toBeDefined();
      await expect.poll(async () => (await debugState(ui, tabId)).orbState, { timeout: 120_000 }).toBe('idle');
      const state = await debugState(ui, tabId);
      const record = (await voiceTurns(ui)).at(-1)!;
      console.log(JSON.stringify({ turn, transcript: record.transcript, caption: state.caption,
        directive: state.lastDirective, latencyMs: record.latencyMs }));
      expect(state.lastDirective?.target).toBe('Show popover');
      await page.waitForTimeout(1_000);
    }
  });
});
