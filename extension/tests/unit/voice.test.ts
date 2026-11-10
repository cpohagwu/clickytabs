import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  base64ToBytes,
  bytesToBase64,
  estimateSpeechMs,
  levelOf,
  MIN_START_BUFFER_MS,
  pcm16ToFloat32,
  startBufferMs,
} from '@/lib/audio/pcm';
import { Player, type PlayerEvent } from '@/lib/audio/player';
import { isAllowed, parseOffscreenRequest, parseSwRequest, parseTabMessage } from '@/lib/messages';

describe('pcm helpers', () => {
  it('decodes little-endian PCM16 and round-trips base64', () => {
    const bytes = new Uint8Array([0x00, 0x40, 0x00, 0xc0, 0xff]);       // 16384, -16384, dangling byte
    expect([...pcm16ToFloat32(bytes)]).toEqual([0.5, -0.5]);
    expect(base64ToBytes(bytesToBase64(bytes))).toEqual(bytes);
  });

  it('maps loudness to an orb level', () => {
    expect(levelOf([])).toBe(0);
    expect(levelOf(new Float32Array(256))).toBe(0);
    expect(levelOf(new Float32Array(256).fill(0.5))).toBe(1);
    const quiet = levelOf(new Float32Array(256).fill(0.003));
    expect(quiet).toBeGreaterThan(0);
    expect(quiet).toBeLessThan(0.2);
  });

  it('buffers just enough that slow synthesis still plays without gaps', () => {
    expect(startBufferMs(3000, 1.2)).toBe(MIN_START_BUFFER_MS);         // faster than real time: start at once
    expect(startBufferMs(3000, 0.6)).toBe(3000 * 0.4 + 200);
    expect(startBufferMs(3000, 0)).toBe(3000);                         // nothing arriving: wait for the whole thing
    expect(estimateSpeechMs('Hi.')).toBe(600);
    expect(estimateSpeechMs('x'.repeat(100))).toBe(6500);
  });
});

// ------------------------------------------------------------------------------------------------- player

interface FakeSource {
  buffer: { duration: number } | null;
  at: number | null;
  stopped: boolean;
  onended: (() => void) | null;
  connect(): void;
  start(at: number): void;
  stop(): void;
}

let sources: FakeSource[] = [];
let ctxTime = 0;

class FakeAudioContext {
  destination = {};
  get currentTime(): number {
    return ctxTime;
  }
  createBuffer(_channels: number, length: number, sampleRate: number) {
    return { duration: length / sampleRate, copyToChannel() {} };
  }
  createBufferSource(): FakeSource {
    const source: FakeSource = {
      buffer: null, at: null, stopped: false, onended: null, connect() {},
      start(at: number) { this.at = at; }, stop() { this.stopped = true; },
    };
    sources.push(source);
    return source;
  }
  createGain() { return { connect() {} }; }
  createAnalyser() { return { fftSize: 1024, connect() {}, getFloatTimeDomainData() {} }; }
  resume() { return Promise.resolve(); }
  decodeAudioData() { return Promise.reject(new Error('not used')); }
}

/** `ms` of 24 kHz PCM16 silence. */
const pcm = (ms: number): Uint8Array => new Uint8Array((24_000 * ms) / 1000 * 2);

describe('Player', () => {
  let events: PlayerEvent[];
  let now: number;

  beforeEach(() => {
    vi.useFakeTimers();
    sources = [];
    ctxTime = 0;
    now = 0;
    events = [];
    vi.stubGlobal('AudioContext', FakeAudioContext);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  const played = (state: string) => events.filter((e) => e.type === 'play.event' && e.state === state)
    .map((e) => (e.type === 'play.event' ? e.seq : -1));

  it('plays complete segments back to back, in order, then reports idle', async () => {
    const player = new Player((e) => events.push(e), () => now);
    player.segment('t', 0, 'audio/pcm', 24_000, 'First sentence.');
    player.segment('t', 1, 'audio/pcm', 24_000, 'Second one.');
    player.audio('t', 1, pcm(400));                                   // the later segment is ready first...
    await player.end('t', 1);
    expect(played('started')).toEqual([]);                            // ...but must wait for segment 0
    player.audio('t', 0, pcm(500));
    await player.end('t', 0);
    expect(played('started')).toEqual([0, 1]);
    const [first, second] = sources;
    expect(second!.at).toBeCloseTo(first!.at! + 0.5, 6);             // gapless
    await vi.advanceTimersByTimeAsync(1_000);
    expect(played('ended')).toEqual([0, 1]);
    expect(played('idle')).toEqual([1]);
  });

  it('waits for enough audio when synthesis is slower than playback', async () => {
    const player = new Player((e) => events.push(e), () => now);
    player.segment('t', 0, 'audio/pcm', 24_000, 'x'.repeat(46));     // ~3 s estimated
    // 200 ms of audio every 300 ms: rate 0.67 -> needs ~1.2 s buffered before it can start safely
    for (let chunk = 0; chunk < 5; chunk += 1) {
      player.audio('t', 0, pcm(200));
      now += 300;
      await vi.advanceTimersByTimeAsync(300);
    }
    expect(played('started')).toEqual([]);
    for (let chunk = 0; chunk < 3; chunk += 1) {
      player.audio('t', 0, pcm(200));
      now += 300;
      await vi.advanceTimersByTimeAsync(300);
    }
    expect(played('started')).toEqual([0]);
  });

  it('stops at once on barge-in and ignores audio of the old turn', async () => {
    const player = new Player((e) => events.push(e), () => now);
    player.segment('t', 0, 'audio/pcm', 24_000, 'Hello there.');
    player.audio('t', 0, pcm(500));
    await player.end('t', 0);
    player.stop();
    expect(played('interrupted')).toEqual([0]);
    expect(sources.every((s) => s.stopped)).toBe(true);
    player.audio('t', 0, pcm(500));
    expect(sources).toHaveLength(1);
  });
});

// ------------------------------------------------------------------------------------------------- messages

describe('voice messages', () => {
  it('validates offscreen traffic and keeps it away from content scripts', () => {
    expect(parseSwRequest({ type: 'rec.chunk', turnId: 't', seq: 3, data: 'AAAA', mime: 'audio/webm' })).toEqual(
      { type: 'rec.chunk', turnId: 't', seq: 3, data: 'AAAA', mime: 'audio/webm' });
    expect(parseSwRequest({ type: 'rec.chunk', turnId: 't', seq: -1, data: 'AAAA', mime: 'x' })).toBeNull();
    expect(parseSwRequest({ type: 'play.event', turnId: 't', seq: 0, state: 'started', inMs: 40 })).toMatchObject(
      { state: 'started', inMs: 40 });
    expect(parseSwRequest({ type: 'play.event', turnId: 't', seq: 0, state: 'paused', inMs: 0 })).toBeNull();
    expect(parseSwRequest({ type: 'rec.level', turnId: 't', level: 2 })).toBeNull();
    expect(isAllowed('rec.chunk', 'content-top')).toBe(false);
    expect(isAllowed('debug.voice', 'content-frame')).toBe(false);
  });

  it('parses recorder/player commands and tab updates', () => {
    expect(parseOffscreenRequest({ target: 'offscreen', type: 'play.segment', turnId: 't', seq: 1,
      mime: 'audio/pcm', sampleRate: 24_000, text: 'Hi.' })).toMatchObject({ type: 'play.segment', sampleRate: 24_000 });
    expect(parseOffscreenRequest({ target: 'offscreen', type: 'rec.start' })).toBeNull();
    expect(parseTabMessage({ type: 'speech.playback', turnId: 't', seq: 0, state: 'idle', inMs: 0 })).toEqual(
      { type: 'speech.playback', turnId: 't', seq: 0, state: 'idle', inMs: 0 });
    expect(parseTabMessage({ type: 'voice.level', source: 'speaker', level: 0.5 })).toBeNull();
    expect(parseTabMessage({ type: 'turn.event', event: { type: 'transcript.final', turn_id: 't', text: 'hi' } }))
      .toMatchObject({ event: { text: 'hi' } });
  });
});
