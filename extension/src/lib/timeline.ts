/**
 * The developer latency timeline under an answer: where the time of the last turn went.
 *
 * Voice turns count from the moment the push-to-talk key was released. The backend reports when the transcript was
 * ready, the brain's first token, the first audio it sent and when it finished (`turn.done.metrics`, measured from
 * `audio.end`); the page adds when the answer became audible. Typed turns count from Enter: first words and done,
 * measured in the page.
 */

export interface TimelineMark {
  label: string;
  ms: number;
}

export interface TurnTimeline {
  kind: 'voice' | 'text';
  marks: TimelineMark[];
}

/** The page's own measurements, in ms from the start of the turn. */
export interface ClientMarks {
  firstText?: number;
  heard?: number;
  done?: number;
}

const SERVER_LABELS: Record<string, string> = {
  stt_ms: 'transcript',
  brain_first_token_ms: 'first words',
  first_audio_ms: 'audio sent',
  total_ms: 'backend done',
};

const MAX_MS = 10 * 60_000;

function valid(ms: number | undefined): ms is number {
  return typeof ms === 'number' && Number.isFinite(ms) && ms >= 0 && ms <= MAX_MS;
}

export function buildTimeline(kind: TurnTimeline['kind'], client: ClientMarks,
  metrics?: Record<string, number | undefined> | null): TurnTimeline | null {
  const marks: TimelineMark[] = [];
  if (kind === 'voice') {
    for (const [key, label] of Object.entries(SERVER_LABELS)) {
      const ms = metrics?.[key];
      if (valid(ms)) marks.push({ label, ms: Math.round(ms) });
    }
    if (valid(client.heard)) marks.push({ label: 'heard', ms: Math.round(client.heard) });
  } else {
    if (valid(client.firstText)) marks.push({ label: 'first words', ms: Math.round(client.firstText) });
    if (valid(client.done)) marks.push({ label: 'done', ms: Math.round(client.done) });
  }
  if (!marks.length) return null;
  marks.sort((a, b) => a.ms - b.ms);
  return { kind, marks };
}

/** "820 ms", "3.1 s" */
export function formatMs(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}
