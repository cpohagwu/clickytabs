/**
 * E2E: push-to-talk end to end on the fake profile.
 *
 * Chromium's fake microphone feeds the real offscreen MediaRecorder; webm/opus chunks reach the real backend; the
 * fake STT/brain/TTS answer; the streamed PCM plays in the real offscreen player, and captions follow playback.
 */
import { debugState, expect, holdToTalk, openFixture, pair, popup, test, voiceTurns } from './fixtures';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const chrome: any;

const PAGE = '<!doctype html><title>voice</title><body style="margin:0"><h1>Voice fixture</h1>'
  + '<p>Hold Right Ctrl and ask something.</p></body>';

test('hold Right Ctrl, speak, hear the answer with captions in sync', async ({ context, sw }) => {
  await pair(context, sw);
  const { page, tabId } = await openFixture(context, { path: 'voice', html: PAGE });
  const ui = await popup(context);
  await page.bringToFront();
  await expect.poll(async () => (await debugState(ui, tabId)).conn).toBe('connected');

  await page.keyboard.down('ControlRight');
  await expect.poll(async () => (await debugState(ui, tabId)).orbState).toBe('listening');
  await page.waitForTimeout(1200);
  await page.keyboard.up('ControlRight');

  await expect.poll(async () => (await voiceTurns(ui)).at(-1)?.outcome, { timeout: 20_000 }).toBe('end');
  const [turn] = (await voiceTurns(ui)).slice(-1);
  expect(turn!.chunks).toBeGreaterThanOrEqual(8);                    // ~100 ms slices for ~1.2 s
  expect(turn!.transcript).toBe('hello from the fake microphone');
  expect(turn!.micStartMs).toBeLessThan(2_000);
  expect(turn!.latencyMs).toBeGreaterThan(0);                         // key release -> audible answer
  expect(turn!.metrics).toMatchObject({ stt_ms: expect.any(Number), first_audio_ms: expect.any(Number) });
  test.info().annotations.push({ type: 'latency', description: JSON.stringify({ latencyMs: turn!.latencyMs,
    micStartMs: turn!.micStartMs, ...turn!.metrics }) });

  await expect
    .poll(async () => (await debugState(ui, tabId)).caption, { timeout: 15_000 })
    .toBe('You said: hello from the fake microphone. This is the fake brain streaming a scripted reply.');
  await expect.poll(async () => (await debugState(ui, tabId)).orbState, { timeout: 15_000 }).toBe('idle');
});

test('a tap without speech ends quietly with a hint, and the microphone is released', async ({ context, sw }) => {
  await pair(context, sw);
  const { page, tabId } = await openFixture(context, { path: 'tap', html: PAGE });
  const ui = await popup(context);
  await page.bringToFront();
  await expect.poll(async () => (await debugState(ui, tabId)).conn).toBe('connected');

  await holdToTalk(page, 230);                                       // just past the 200 ms arm
  await expect.poll(async () => (await voiceTurns(ui)).at(-1)?.outcome, { timeout: 15_000 }).toMatch(/end|error/);
  await expect.poll(async () => (await debugState(ui, tabId)).orbState, { timeout: 10_000 }).not.toBe('listening');
  // The recorder let go of the microphone: a fresh probe can open it again.
  expect(await ui.evaluate(() => chrome.runtime.sendMessage({ type: 'debug.micProbe' }))).toEqual({ ok: true });
});

test('speaking again while it answers cuts the answer off (barge-in)', async ({ context, sw }) => {
  await pair(context, sw);
  const { page, tabId } = await openFixture(context, { path: 'barge', html: PAGE });
  const ui = await popup(context);
  await page.bringToFront();
  await expect.poll(async () => (await debugState(ui, tabId)).conn).toBe('connected');

  await holdToTalk(page, 900);
  await expect.poll(async () => (await voiceTurns(ui)).at(-1)?.firstAudioAt, { timeout: 20_000 }).toBeDefined();
  await holdToTalk(page, 900);                                       // press again as soon as it speaks
  await expect.poll(async () => (await voiceTurns(ui)).length, { timeout: 10_000 }).toBe(2);
  await expect.poll(async () => (await voiceTurns(ui)).at(-1)?.outcome, { timeout: 20_000 }).toBe('end');
  const [first, second] = await voiceTurns(ui);
  expect(['cancelled', 'end']).toContain(first!.outcome);            // 'end' only if it had already finished
  expect(second!.transcript).toBe('hello from the fake microphone');
});
