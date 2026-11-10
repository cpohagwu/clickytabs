/**
 * Push-to-talk recorder (offscreen document): MediaRecorder webm/opus in 100 ms slices, plus a mic level meter.
 *
 * The microphone is opened when recording starts and released as soon as it stops, so Chrome's mic indicator is only
 * on while the user holds the key. Chunk messages are emitted strictly in order, and `rec.done` only after the last
 * slice: the worker can then send `audio.end` without ever overtaking audio.
 */
import { bytesToBase64, levelOf } from './pcm';

export type RecorderEvent =
  | { type: 'rec.chunk'; turnId: string; seq: number; data: string; mime: string }
  | { type: 'rec.level'; turnId: string; level: number }
  | { type: 'rec.done'; turnId: string; chunks: number; durationMs: number };

export type RecorderStart = { ok: true; startMs: number } | { ok: false; error: string; name: string };

const SLICE_MS = 100;
const LEVEL_MS = 66;
const MIME = 'audio/webm;codecs=opus';

export class Recorder {
  private stream: MediaStream | null = null;
  private recorder: MediaRecorder | null = null;
  private context: AudioContext | null = null;
  private levelTimer: ReturnType<typeof setInterval> | null = null;
  private queue: Promise<void> = Promise.resolve();
  private turnId: string | null = null;
  /** A stop/cancel that arrived while getUserMedia was still opening the mic (a quick tap). */
  private starting: { turnId: string; stop: boolean; cancel: boolean } | null = null;

  constructor(private readonly emit: (event: RecorderEvent) => void) {}

  get recording(): boolean {
    return this.recorder !== null;
  }

  async start(turnId: string): Promise<RecorderStart> {
    this.cancel();
    const started = performance.now();
    const starting = { turnId, stop: false, cancel: false };
    this.starting = starting;
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
    } catch (error) {
      this.starting = null;
      const name = error instanceof DOMException ? error.name : 'Error';
      return { ok: false, name, error: error instanceof Error ? error.message : String(error) };
    }
    this.starting = null;
    if (starting.cancel) {
      for (const track of this.stream.getTracks()) track.stop();
      this.stream = null;
      return { ok: false, name: 'AbortError', error: 'cancelled while the microphone was opening' };
    }
    this.turnId = turnId;
    const recorder = new MediaRecorder(this.stream, { mimeType: MIME, audioBitsPerSecond: 32_000 });
    this.recorder = recorder;
    let seq = 0;
    const recordingStarted = performance.now();
    recorder.ondataavailable = (event: BlobEvent): void => {
      if (event.data.size === 0) return;
      const index = seq;
      seq += 1;
      const blob = event.data;
      this.queue = this.queue.then(async () => {
        const data = bytesToBase64(new Uint8Array(await blob.arrayBuffer()));
        this.emit({ type: 'rec.chunk', turnId, seq: index, data, mime: recorder.mimeType || MIME });
      });
    };
    recorder.onstop = (): void => {
      const durationMs = Math.round(performance.now() - recordingStarted);
      this.queue = this.queue.then(() => this.emit({ type: 'rec.done', turnId, chunks: seq, durationMs }));
      this.release();
    };
    recorder.start(SLICE_MS);
    this.meter(turnId);
    if (starting.stop) this.stop(turnId);              // released before the mic was even open
    return { ok: true, startMs: Math.round(performance.now() - started) };
  }

  /** Stop recording `turnId` (its last slice and `rec.done` follow). */
  stop(turnId: string): void {
    if (this.starting?.turnId === turnId) {
      this.starting.stop = true;
      return;
    }
    if (this.turnId !== turnId || !this.recorder) return;
    if (this.recorder.state !== 'inactive') this.recorder.stop();
    else this.release();
  }

  /** Drop the current recording without a `rec.done` (barge-in of a recording that never became a turn). */
  cancel(): void {
    if (this.starting) this.starting.cancel = true;
    if (!this.recorder) return;
    this.recorder.ondataavailable = null;
    this.recorder.onstop = null;
    if (this.recorder.state !== 'inactive') this.recorder.stop();
    this.release();
  }

  private meter(turnId: string): void {
    if (!this.stream) return;
    try {
      this.context = new AudioContext();
      const analyser = this.context.createAnalyser();
      analyser.fftSize = 1024;
      this.context.createMediaStreamSource(this.stream).connect(analyser);
      const samples = new Float32Array(analyser.fftSize);
      this.levelTimer = setInterval(() => {
        analyser.getFloatTimeDomainData(samples);
        this.emit({ type: 'rec.level', turnId, level: Math.round(levelOf(samples) * 100) / 100 });
      }, LEVEL_MS);
    } catch {
      // metering is cosmetic
    }
  }

  private release(): void {
    if (this.levelTimer !== null) clearInterval(this.levelTimer);
    this.levelTimer = null;
    void this.context?.close().catch(() => undefined);
    this.context = null;
    for (const track of this.stream?.getTracks() ?? []) track.stop();   // mic indicator off
    this.stream = null;
    this.recorder = null;
    this.turnId = null;
  }
}
