/**
 * Gapless speech player (offscreen document).
 *
 * Segments (sentences) of one turn play strictly in order; each streams in as PCM16 chunks that are scheduled
 * back-to-back on one AudioContext clock. A segment starts once `startBufferMs` says the rest will arrive in time
 * (or once it is complete); the next one starts exactly where the previous ends. Complete-file mimes (wav/mp3/ogg)
 * are decoded when the segment ends. Events say when a segment *becomes audible* (`inMs` from now), which is what
 * captions and pointer timing follow; `idle` means everything received so far has finished playing.
 */
import { estimateSpeechMs, levelOf, pcm16ToFloat32, pcmDurationMs, startBufferMs } from './pcm';

export type PlaybackState = 'started' | 'ended' | 'interrupted' | 'idle';

export type PlayerEvent =
  | { type: 'play.event'; turnId: string; seq: number; state: PlaybackState; inMs: number; durationMs?: number }
  | { type: 'play.level'; turnId: string; level: number };

interface Segment {
  seq: number;
  pcm: boolean;
  sampleRate: number;
  estimatedMs: number;
  pending: AudioBuffer[];
  pendingMs: number;
  receivedMs: number;
  firstChunkMs: number;
  firstAt: number | null;
  raw: Uint8Array[];
  ended: boolean;
  started: boolean;
  durationMs: number;
}

const LEVEL_MS = 66;
const RECHECK_MS = 60;
const START_LEAD_S = 0.04;

export class Player {
  private ctx: AudioContext | null = null;
  private output: GainNode | null = null;
  private analyser: AnalyserNode | null = null;
  private turnId: string | null = null;
  private segments = new Map<number, Segment>();
  private current = 0;
  private nextTime = 0;
  /** The segment being heard (fully scheduled segments are still audible until their end). */
  private audible: number | null = null;
  private sources = new Set<AudioBufferSourceNode>();
  private timers = new Set<ReturnType<typeof setTimeout>>();
  private recheck: ReturnType<typeof setTimeout> | null = null;
  private levelTimer: ReturnType<typeof setInterval> | null = null;

  constructor(
    private readonly emit: (event: PlayerEvent) => void,
    private readonly now: () => number = () => performance.now(),
  ) {}

  segment(turnId: string, seq: number, mime: string, sampleRate: number | null, text: string): void {
    this.ensureTurn(turnId);
    this.segments.set(seq, {
      seq, pcm: mime.startsWith('audio/pcm'), sampleRate: sampleRate ?? 24_000, estimatedMs: estimateSpeechMs(text),
      pending: [], pendingMs: 0, receivedMs: 0, firstChunkMs: 0, firstAt: null, raw: [], ended: false,
      started: false, durationMs: 0,
    });
    this.pump();
  }

  audio(turnId: string, seq: number, bytes: Uint8Array): void {
    const segment = this.segments.get(seq);
    if (turnId !== this.turnId || !segment || !this.ctx) return;
    if (!segment.pcm) {
      segment.raw.push(bytes);
      return;
    }
    const samples = pcm16ToFloat32(bytes);
    if (samples.length === 0) return;
    const buffer = this.ctx.createBuffer(1, samples.length, segment.sampleRate);
    buffer.copyToChannel(samples, 0);
    const ms = pcmDurationMs(samples.length * 2, segment.sampleRate);
    if (segment.firstAt === null) {
      segment.firstAt = this.now();
      segment.firstChunkMs = ms;
    }
    segment.pending.push(buffer);
    segment.pendingMs += ms;
    segment.receivedMs += ms;
    segment.durationMs += ms;
    this.pump();
  }

  async end(turnId: string, seq: number): Promise<void> {
    const segment = this.segments.get(seq);
    if (turnId !== this.turnId || !segment || !this.ctx) return;
    if (!segment.pcm && segment.raw.length) {
      const joined = new Uint8Array(segment.raw.reduce((n, part) => n + part.length, 0));
      let offset = 0;
      for (const part of segment.raw) {
        joined.set(part, offset);
        offset += part.length;
      }
      try {
        const decoded = await this.ctx.decodeAudioData(joined.buffer);
        segment.pending.push(decoded);
        segment.durationMs = decoded.duration * 1000;
      } catch {
        // undecodable segment: skip it, keep the turn going
      }
    }
    segment.ended = true;
    this.pump();
  }

  /** Barge-in: silence now. */
  stop(): void {
    if (this.turnId && this.audible !== null) {
      this.emit({ type: 'play.event', turnId: this.turnId, seq: this.audible, state: 'interrupted', inMs: 0 });
    }
    this.audible = null;
    for (const source of this.sources) {
      source.onended = null;
      try {
        source.stop();
      } catch {
        // already stopped
      }
    }
    this.sources.clear();
    for (const timer of this.timers) clearTimeout(timer);
    this.timers.clear();
    if (this.recheck !== null) clearTimeout(this.recheck);
    this.recheck = null;
    this.stopLevel();
    this.segments.clear();
    this.turnId = null;
    this.current = 0;
    this.nextTime = 0;
  }

  private ensureTurn(turnId: string): void {
    if (this.turnId === turnId) return;
    this.stop();
    this.turnId = turnId;
    if (!this.ctx) {
      this.ctx = new AudioContext({ latencyHint: 'interactive' });
      this.output = this.ctx.createGain();
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.output.connect(this.analyser);
      this.analyser.connect(this.ctx.destination);
    }
    void this.ctx.resume().catch(() => undefined);
  }

  /** Whether a segment can start now without running dry (see startBufferMs). */
  private ready(segment: Segment, leadMs: number): boolean {
    if (segment.ended) return segment.pending.length > 0 || segment.durationMs === 0;
    if (segment.firstAt === null) return false;
    const elapsed = this.now() - segment.firstAt;
    if (elapsed < 120) return false;                        // no arrival rate yet
    const rate = (segment.receivedMs - segment.firstChunkMs) / elapsed;
    const total = Math.max(segment.estimatedMs, segment.receivedMs);
    return segment.pendingMs + leadMs * Math.min(1, rate) >= startBufferMs(total, rate);
  }

  private pump(): void {
    const ctx = this.ctx;
    const turnId = this.turnId;
    if (!ctx || !this.output || !turnId) return;
    for (;;) {
      const segment = this.segments.get(this.current);
      if (!segment) return;
      const leadMs = Math.max(0, this.nextTime - ctx.currentTime) * 1000;
      if (!segment.started) {
        if (!this.ready(segment, leadMs)) {
          this.scheduleRecheck();
          return;
        }
        if (segment.ended && segment.pending.length === 0) {          // nothing to play
          this.current += 1;
          continue;
        }
        segment.started = true;
        this.audible = segment.seq;
        const startAt = Math.max(ctx.currentTime + START_LEAD_S, this.nextTime);
        this.nextTime = startAt;
        this.emit({ type: 'play.event', turnId, seq: segment.seq, state: 'started',
          inMs: Math.round((startAt - ctx.currentTime) * 1000) });
        this.startLevel();
      }
      for (const buffer of segment.pending) {
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.connect(this.output);
        const at = Math.max(this.nextTime, ctx.currentTime + 0.02);   // an underrun resumes as soon as audio arrives
        source.start(at);
        this.nextTime = at + buffer.duration;
        this.sources.add(source);
        source.onended = () => this.sources.delete(source);
      }
      segment.pending = [];
      segment.pendingMs = 0;
      if (!segment.ended) return;
      const endsInMs = Math.max(0, (this.nextTime - ctx.currentTime) * 1000);
      const seq = segment.seq;
      const durationMs = Math.round(segment.durationMs);
      this.later(endsInMs, () => {
        if (this.audible === seq) this.audible = null;
        this.emit({ type: 'play.event', turnId, seq, state: 'ended', inMs: 0, durationMs });
        if (!this.segments.has(seq + 1) && this.turnId === turnId) {
          this.emit({ type: 'play.event', turnId, seq, state: 'idle', inMs: 0 });
          this.stopLevel();
        }
      });
      this.current += 1;
    }
  }

  private later(ms: number, fn: () => void): void {
    const timer = setTimeout(() => {
      this.timers.delete(timer);
      fn();
    }, ms);
    this.timers.add(timer);
  }

  private scheduleRecheck(): void {
    if (this.recheck !== null) return;
    this.recheck = setTimeout(() => {
      this.recheck = null;
      this.pump();
    }, RECHECK_MS);
  }

  private startLevel(): void {
    if (this.levelTimer !== null || !this.analyser) return;
    const analyser = this.analyser;
    const samples = new Float32Array(analyser.fftSize);
    this.levelTimer = setInterval(() => {
      if (!this.turnId) return;
      analyser.getFloatTimeDomainData(samples);
      this.emit({ type: 'play.level', turnId: this.turnId, level: Math.round(levelOf(samples) * 100) / 100 });
    }, LEVEL_MS);
  }

  private stopLevel(): void {
    if (this.levelTimer !== null) clearInterval(this.levelTimer);
    this.levelTimer = null;
  }
}
