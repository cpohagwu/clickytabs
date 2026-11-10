/**
 * Opt-in acceptance: latency from key release to the first audible answer, on a real backend.
 *
 *   # a backend on the profile to measure, with a throw-away token
 *   $env:CLICKY_HOME = "$env:TEMP\clicky-latency"; $env:CLICKY_AUTH_TOKEN = 'latency-token-0123456789'
 *   $env:CLICKY_ALLOWED_EXTENSION_IDS = 'fhgliaahcafninaodeelekpdemjkifmf'
 *   uv run --directory ../backend clicky serve --port 18766 --profile local
 *
 *   # a spoken question as 16-bit mono WAV (Chromium's fake microphone plays it)
 *   $env:CC_E2E_MIC_WAV = 'C:\path\question.wav'; $env:CC_E2E_VOICE_BACKEND = '18766:latency-token-0123456789'
 *   npx playwright test voice-latency
 *
 * Each turn asks about a fixture page (page context included, as in real use) and the worker records key release ->
 * first audible answer plus the backend's breakdown. The p50 lands in test-results/voice-latency.json.
 */
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { debugState, expect, holdToTalk, openFixture, pair, popup, test, voiceTurns } from './fixtures';

const TURNS = Number(process.env.CC_E2E_VOICE_TURNS ?? 5);
const HOLD_MS = Number(process.env.CC_E2E_VOICE_HOLD_MS ?? 3_000);
const [port, token] = (process.env.CC_E2E_VOICE_BACKEND ?? '').split(':');

const PAGE = `<!doctype html><html lang="en"><head><title>Popover API - Web APIs</title></head>
<body style="margin:0;font:16px sans-serif">
<header><nav><a href="#">Docs</a> <a href="#">Guides</a> <a href="#">Reference</a></nav></header>
<main><h1>Popover API</h1>
<p>The Popover API provides developers with a standard, consistent, flexible mechanism for displaying popover content
on top of other page content.</p><button>Show popover</button></main></body></html>`;

const median = (values: number[]): number => {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid]! : (sorted[mid - 1]! + sorted[mid]!) / 2;
};

test.describe('voice latency', () => {
  test.skip(!process.env.CC_E2E_MIC_WAV || !port, 'set CC_E2E_MIC_WAV and CC_E2E_VOICE_BACKEND (see the header)');
  test.use({ micFile: process.env.CC_E2E_MIC_WAV ?? null });
  test.setTimeout(10 * 60_000);

  test(`key release -> first audio over ${TURNS} turns`, async ({ context, sw }) => {
    await pair(context, sw, { port: Number(port), token: token! });
    const { page, tabId } = await openFixture(context, { path: 'latency', html: PAGE });
    const ui = await popup(context);
    await page.bringToFront();
    await expect.poll(async () => (await debugState(ui, tabId)).conn).toBe('connected');

    for (let turn = 1; turn <= TURNS; turn += 1) {
      await holdToTalk(page, HOLD_MS);
      await expect.poll(async () => (await voiceTurns(ui)).length, { timeout: 10_000 }).toBe(turn);
      await expect.poll(async () => (await voiceTurns(ui)).at(-1)?.outcome, { timeout: 120_000 }).toBeDefined();
      await expect.poll(async () => (await debugState(ui, tabId)).orbState, { timeout: 120_000 }).toBe('idle');
      await page.waitForTimeout(1_000);
    }

    const turns = await voiceTurns(ui);
    const answered = turns.filter((t) => t.outcome === 'end' && t.latencyMs !== undefined);
    const summary = {
      turns: turns.map((t) => ({ outcome: t.outcome, transcript: t.transcript, latencyMs: t.latencyMs,
        micStartMs: t.micStartMs, ...t.metrics, error: t.error })),
      p50LatencyMs: answered.length ? median(answered.map((t) => t.latencyMs!)) : null,
      p50: Object.fromEntries(['stt_ms', 'brain_first_token_ms', 'first_audio_ms', 'total_ms'].map((key) => [key,
        median(answered.map((t) => t.metrics?.[key] ?? 0))])),
    };
    writeFileSync(fileURLToPath(new URL('../../test-results/voice-latency.json', import.meta.url)),
      JSON.stringify(summary, null, 2));
    console.log(JSON.stringify(summary, null, 2));
    expect(answered.length).toBe(TURNS);
  });
});
