import { describe, expect, it } from 'vitest';

import { isAllowed, MAX_TURN_TEXT_CHARS, parseSwRequest, parseTabMessage } from '@/lib/messages';
import { normalizeSettings, toPagePrefs, validateSettingsPatch } from '@/lib/settings';

describe('HUD turn + demo messages', () => {
  it('parses demo.run with a tab id only', () => {
    expect(parseSwRequest({ type: 'demo.run', tabId: 7 })).toEqual({ type: 'demo.run', tabId: 7 });
    expect(parseSwRequest({ type: 'demo.run' })).toBeNull();
    expect(parseSwRequest({ type: 'demo.run', reason: 'keyup' })).toBeNull();
  });

  it('trims turn.text and enforces its bounds', () => {
    expect(parseSwRequest({ type: 'turn.text', text: '  hi  ' })).toEqual({ type: 'turn.text', text: 'hi' });
    expect(parseSwRequest({ type: 'turn.text', text: '   ' })).toBeNull();
    expect(parseSwRequest({ type: 'turn.text', text: 'x'.repeat(MAX_TURN_TEXT_CHARS + 1) })).toBeNull();
    expect(parseSwRequest({ type: 'turn.text', text: 42 })).toBeNull();
    expect(parseSwRequest({ type: 'turn.stop', extra: true })).toEqual({ type: 'turn.stop' });
  });

  it('lets only the top frame start HUD turns and only extension pages start the demo', () => {
    expect(isAllowed('turn.text', 'content-top')).toBe(true);
    expect(isAllowed('turn.text', 'content-frame')).toBe(false);
    expect(isAllowed('turn.stop', 'content-frame')).toBe(false);
    expect(isAllowed('demo.run', 'content-top')).toBe(false);
    expect(isAllowed('demo.run', 'extension-page')).toBe(true);
  });

  it('accepts forwarded turn events and rejects malformed ones', () => {
    const delta = { type: 'assistant.text.delta', turn_id: 't1', delta: 'Hi' };
    expect(parseTabMessage({ type: 'turn.event', event: delta })).toEqual({ type: 'turn.event', event: delta });
    expect(parseTabMessage({ type: 'turn.event', event: { type: 'assistant.text.delta', delta: 'x' } })).toBeNull();
    expect(parseTabMessage({ type: 'turn.event', event: { type: 'welcome', turn_id: 't1' } })).toBeNull();
    expect(parseTabMessage({ type: 'turn.event', event: { type: 'visual.directive', turn_id: 't1' } })).toBeNull();
    const error = { type: 'error', code: 'x', message: 'boom' };
    expect(parseTabMessage({ type: 'turn.event', event: error })).toEqual({ type: 'turn.event', event: error });
    expect(parseTabMessage({ type: 'demo.run' })).toEqual({ type: 'demo.run' });
  });
});

describe('companion mode setting', () => {
  it('defaults to dock, validates patches and reaches extension pages', () => {
    const settings = normalizeSettings({});
    expect(settings.companionMode).toBe('dock');
    expect(normalizeSettings({ companionMode: 'sideways' }).companionMode).toBe('dock');
    expect(validateSettingsPatch({ companionMode: 'follow' })).toMatchObject({ ok: true });
    expect(toPagePrefs(normalizeSettings({ companionMode: 'follow' })).companionMode).toBe('follow');
    expect(parseSwRequest({ type: 'settings.set', patch: { companionMode: 'nope' } })).toBeNull();
  });
});
