/** The developer latency timeline under an answer, and its settings toggle. */
import { describe, expect, it } from 'vitest';

import { parseSettingsPatch } from '@/lib/messages';
import { normalizeSettings, validateSettingsPatch } from '@/lib/settings';
import { buildTimeline, formatMs } from '@/lib/timeline';

describe('latency timeline', () => {
  it('orders a voice turn from key release: the backend marks plus when it became audible', () => {
    const timeline = buildTimeline('voice', { heard: 2950.4, firstText: 10 },
      { stt_ms: 812.2, brain_first_token_ms: 1890, first_audio_ms: 2400, total_ms: 4100, other_ms: 5 });
    expect(timeline).toEqual({
      kind: 'voice',
      marks: [
        { label: 'transcript', ms: 812 },
        { label: 'first words', ms: 1890 },
        { label: 'audio sent', ms: 2400 },
        { label: 'heard', ms: 2950 },
        { label: 'backend done', ms: 4100 },
      ],
    });
  });

  it('times a typed turn from Enter, and drops what is missing or nonsense', () => {
    expect(buildTimeline('text', { firstText: 640, done: 1220 })?.marks)
      .toEqual([{ label: 'first words', ms: 640 }, { label: 'done', ms: 1220 }]);
    expect(buildTimeline('voice', { heard: -5 }, { stt_ms: Number.NaN, total_ms: 3_600_000 })).toBeNull();
    expect(buildTimeline('text', {})).toBeNull();
    expect([formatMs(820.4), formatMs(3140)]).toEqual(['820 ms', '3.1 s']);
  });

  it('is off by default and switched on only by a real boolean from an extension page', () => {
    expect(normalizeSettings({}).devTimeline).toBe(false);
    expect(normalizeSettings({ devTimeline: 'yes' }).devTimeline).toBe(false);
    expect(parseSettingsPatch({ devTimeline: true })).toEqual({ devTimeline: true });
    expect(parseSettingsPatch({ devTimeline: 1 })).toBeNull();
    expect(validateSettingsPatch({ devTimeline: true })).toEqual({ ok: true, values: { devTimeline: true } });
  });
});
