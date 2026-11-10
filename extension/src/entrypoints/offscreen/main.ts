/**
 * Offscreen document: owns the microphone (push-to-talk recorder) and speech playback (gapless player).
 *
 * It only has chrome.runtime, and getUserMedia here can never show a permission prompt: the grant must already
 * exist for the extension origin (onboarding page). A NotAllowedError therefore means "send the user to onboarding".
 * Everything it learns (audio chunks, levels, playback progress) goes to the service worker, which routes it on.
 */
import { browser } from 'wxt/browser';

import { base64ToBytes } from '@/lib/audio/pcm';
import { Player, type PlayerEvent } from '@/lib/audio/player';
import { Recorder, type RecorderEvent } from '@/lib/audio/recorder';
import type { Ack, MicProbeResult, OffscreenPingReply, RecStartReply } from '@/lib/messages';
import { OFFSCREEN_LIFELINE_PORT, parseOffscreenRequest } from '@/lib/messages';

const MIC_PROBE_MS = 1_000;
/** The worker releases push-to-talk after 60 s; this is the document's own limit if no worker is left to do it. */
const MAX_RECORDING_MS = 65_000;
const RELINK_DELAY_MS = 200;

function toWorker(event: RecorderEvent | PlayerEvent): void {
  void browser.runtime.sendMessage(event).then((reply: unknown) => {
    // The worker no longer knows this turn (it restarted, or gave the turn up): stop listening at once.
    if (event.type === 'rec.chunk' && (reply as Partial<Ack> | undefined)?.ok === false) recorder.cancel();
  }, () => undefined); // the worker may be restarting
}

const recorder = new Recorder(toWorker);
const player = new Player(toWorker);
let recordingCap: ReturnType<typeof setTimeout> | null = null;

/**
 * Lifeline to the worker. Its disconnect means the worker that owned the recording and the playback is gone:
 * release the microphone and go quiet, then reconnect (which starts a new worker right away, and it reconnects to
 * the backend). A fast-failing link backs off.
 */
let relinkDelay = RELINK_DELAY_MS;
function linkToWorker(): void {
  const linkedAt = Date.now();
  const port = browser.runtime.connect({ name: OFFSCREEN_LIFELINE_PORT });
  port.onDisconnect.addListener(() => {
    recorder.cancel();
    player.stop();
    relinkDelay = Date.now() - linkedAt < 2_000 ? Math.min(relinkDelay * 2, 30_000) : RELINK_DELAY_MS;
    setTimeout(linkToWorker, relinkDelay);
  });
}
linkToWorker();

async function probeMic(): Promise<MicProbeResult> {
  if (recorder.recording) return { ok: true };            // in use right now: obviously granted
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    await new Promise((resolve) => setTimeout(resolve, MIC_PROBE_MS));
    for (const track of stream.getTracks()) track.stop();
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error instanceof DOMException ? error.name : String(error) };
  }
}

browser.runtime.onMessage.addListener((raw: unknown, _sender, sendResponse) => {
  const request = parseOffscreenRequest(raw);
  if (!request) return undefined;
  const ack = (): void => sendResponse({ ok: true } satisfies Ack);
  switch (request.type) {
    case 'ping':
      sendResponse({ ok: true, type: 'pong', at: Date.now(), recording: recorder.recording } satisfies OffscreenPingReply);
      return undefined;
    case 'mic.probe':
      void probeMic().then(sendResponse);
      return true;
    case 'rec.start':
      player.stop();                                       // talking over the answer silences it at once
      void recorder.start(request.turnId).then((reply: RecStartReply) => {
        if (reply.ok) {
          if (recordingCap !== null) clearTimeout(recordingCap);
          recordingCap = setTimeout(() => recorder.stop(request.turnId), MAX_RECORDING_MS);
        }
        sendResponse(reply);
      });
      return true;
    case 'rec.stop':
      recorder.stop(request.turnId);
      ack();
      return undefined;
    case 'rec.cancel':
      recorder.cancel();
      ack();
      return undefined;
    case 'play.segment':
      player.segment(request.turnId, request.seq, request.mime, request.sampleRate, request.text);
      ack();
      return undefined;
    case 'play.audio':
      player.audio(request.turnId, request.seq, base64ToBytes(request.data));
      ack();
      return undefined;
    case 'play.end':
      void player.end(request.turnId, request.seq).then(ack);
      return true;
    case 'play.stop':
      player.stop();
      ack();
      return undefined;
  }
});
