/**
 * Pure audio helpers shared by the offscreen recorder and player (unit-tested).
 *
 * Speech arrives as PCM16 mono (`audio/pcm` + sample rate) in base64 chunks. Local TTS can generate slower than real
 * time while the brain is still decoding (measured RTF 1.4-1.6 on the reference laptop), so the player keeps an
 * adaptive jitter buffer: it starts a segment once enough audio is buffered that the rest, arriving at the measured
 * rate, will keep up. That is gapless at the cost of a later start only when synthesis is slow.
 */

export function base64ToBytes(b64: string): Uint8Array {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

/** Little-endian PCM16 -> float samples in [-1, 1). An odd trailing byte is dropped. */
export function pcm16ToFloat32(bytes: Uint8Array): Float32Array<ArrayBuffer> {
  const count = bytes.length >> 1;
  const view = new DataView(bytes.buffer, bytes.byteOffset, count * 2);
  const out = new Float32Array(count);
  for (let i = 0; i < count; i += 1) out[i] = view.getInt16(i * 2, true) / 32768;
  return out;
}

export function pcmDurationMs(byteLength: number, sampleRate: number): number {
  return (byteLength / 2 / sampleRate) * 1000;
}

/** RMS of samples mapped to 0..1 for the orb (-60 dBFS -> 0, -12 dBFS -> 1). */
export function levelOf(samples: ArrayLike<number>): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) sum += samples[i]! * samples[i]!;
  const rms = Math.sqrt(sum / samples.length);
  if (rms <= 0) return 0;
  const db = 20 * Math.log10(rms);
  return Math.min(1, Math.max(0, (db + 60) / 48));
}

/** Rough spoken length of a sentence before its audio is known (English, ~15 chars/s). */
export function estimateSpeechMs(text: string): number {
  return Math.max(600, text.length * 65);
}

export const MIN_START_BUFFER_MS = 250;
const SAFETY_MS = 200;

/**
 * Audio (ms) to buffer before a segment starts so the rest arrives in time:
 * remaining = total - buffered must arrive within buffered + remaining of playback, given `rate` ms of audio per ms
 * of wall time => buffered >= total * (1 - rate). Never below MIN_START_BUFFER_MS, never above the whole segment.
 */
export function startBufferMs(estimatedTotalMs: number, arrivalRate: number): number {
  const rate = Math.max(0, Math.min(1, arrivalRate));
  const needed = estimatedTotalMs * (1 - rate) + SAFETY_MS;
  return Math.min(Math.max(needed, MIN_START_BUFFER_MS), Math.max(estimatedTotalMs, MIN_START_BUFFER_MS));
}
