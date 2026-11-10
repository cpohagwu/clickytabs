import { describe, expect, it } from 'vitest';

import { classifySender, isAllowed, parseSwRequest, REQUEST_POLICY, type SwRequestType } from '@/lib/messages';
import { validateSettingsPatch } from '@/lib/settings';
import {
  BACKOFF_CAP_MS,
  backoffDelay,
  CLOSE_ABNORMAL,
  CLOSE_FORBIDDEN,
  CLOSE_UNAUTHORIZED,
  classifyClose,
  wantsReconnectAlarm,
} from '@/lib/socket';
import { checkBackendUrl, healthUrlFor, mayCarryToken } from '@/lib/url-policy';

const ID = 'fhgliaahcafninaodeelekpdemjkifmf';

describe('reconnect policy', () => {
  it('backs off exponentially and caps at 25 s (with jitter)', () => {
    const mid = () => 0.5;
    expect(backoffDelay(0, mid)).toBeLessThanOrEqual(1_200);
    for (let attempt = 0; attempt < 30; attempt += 1) {
      expect(backoffDelay(attempt, () => 1)).toBeLessThanOrEqual(BACKOFF_CAP_MS);
    }
    expect(backoffDelay(20, mid)).toBe(BACKOFF_CAP_MS);
  });

  it('treats 4401 as terminal and 4403 as a rejected handshake', () => {
    expect(classifyClose(CLOSE_UNAUTHORIZED, 'open')).toBe('unauthorized');
    expect(classifyClose(CLOSE_FORBIDDEN, 'handshake')).toBe('rejected');
  });

  it('probes /healthz when the connection fails before the handshake (HTTP 403 shows up as 1006)', () => {
    expect(classifyClose(CLOSE_ABNORMAL, 'connecting')).toBe('probe');
    expect(classifyClose(CLOSE_ABNORMAL, 'handshake')).toBe('probe');
    expect(classifyClose(CLOSE_ABNORMAL, 'open')).toBe('retry');
  });

  it('keeps the reconnect alarm while a connection is pending, and as a watchdog while connected', () => {
    expect(wantsReconnectAlarm('disconnected')).toBe(true);
    expect(wantsReconnectAlarm('connecting')).toBe(true);
    expect(wantsReconnectAlarm('connected')).toBe(true);
    expect(wantsReconnectAlarm('unauthorized')).toBe(false);
    expect(wantsReconnectAlarm('unpaired')).toBe(false);
  });
});

describe('backend URL policy', () => {
  it('accepts loopback ws:// and remote wss://', () => {
    expect(checkBackendUrl('ws://127.0.0.1:8765/v1/ws').ok).toBe(true);
    expect(checkBackendUrl('wss://clicky.example.com/v1/ws').ok).toBe(true);
  });

  it('rejects http(s) and other schemes', () => {
    for (const url of ['http://127.0.0.1:8765', 'https://x.example', 'javascript:alert(1)', 'not a url']) {
      expect(checkBackendUrl(url).ok).toBe(false);
    }
  });

  it('never sends the token over plaintext to a non-loopback host', () => {
    expect(mayCarryToken('ws://127.0.0.1:8765/v1/ws')).toBe(true);
    expect(mayCarryToken('ws://localhost:8765/v1/ws')).toBe(true);
    expect(mayCarryToken('ws://192.168.1.20:8765/v1/ws')).toBe(false);
    expect(mayCarryToken('wss://clicky.example.com/v1/ws')).toBe(true);
  });

  it('derives the health URL', () => {
    expect(healthUrlFor('ws://127.0.0.1:8765/v1/ws')).toBe('http://127.0.0.1:8765/healthz');
    expect(healthUrlFor('wss://h.example/v1/ws')).toBe('https://h.example/healthz');
  });
});

describe('sender rules', () => {
  const page = { id: ID, url: `chrome-extension://${ID}/popup.html` };
  const top = { id: ID, tab: { id: 3 }, frameId: 0, url: 'https://news.example/' };
  const frame = { id: ID, tab: { id: 3 }, frameId: 7, url: 'about:blank' };

  it('classifies senders', () => {
    expect(classifySender(page, ID)).toBe('extension-page');
    expect(classifySender(top, ID)).toBe('content-top');
    expect(classifySender(frame, ID)).toBe('content-frame');
    expect(classifySender({ ...top, id: 'someone-else' }, ID)).toBe('foreign');
    expect(classifySender({ id: ID, url: 'https://evil.example/' }, ID)).toBe('foreign');
  });

  it('lets frames relay PTT only, and keeps settings writes and debug hooks for extension pages', () => {
    const frameAllowed = (Object.keys(REQUEST_POLICY) as SwRequestType[]).filter((t) => isAllowed(t, 'content-frame'));
    expect(frameAllowed.sort()).toEqual(['ptt.down', 'ptt.up', 'settings.get', 'status.get']);
    for (const type of ['settings.set', 'settings.read', 'debug.state', 'debug.micProbe'] as SwRequestType[]) {
      expect(isAllowed(type, 'content-top')).toBe(false);
      expect(isAllowed(type, 'extension-page')).toBe(true);
    }
    expect(isAllowed('ptt.down', 'foreign')).toBe(false);
  });

  it('parses only well-formed requests', () => {
    expect(parseSwRequest({ type: 'ptt.up', reason: 'blur' })).toEqual({ type: 'ptt.up', reason: 'blur' });
    expect(parseSwRequest({ type: 'ptt.up', reason: 'whatever' })).toBeNull();
    expect(parseSwRequest({ type: 'settings.set', patch: { token: 42 } })).toBeNull();
    expect(parseSwRequest('ptt.down')).toBeNull();
  });
});

describe('settings validation', () => {
  it('rejects bad tokens and URLs, normalises disabled sites', () => {
    expect(validateSettingsPatch({ token: 'has space' }).ok).toBe(false);
    expect(validateSettingsPatch({ backendUrl: 'http://127.0.0.1:8765' }).ok).toBe(false);
    const result = validateSettingsPatch({ disabledSites: ['https://www.Example.com/path', 'example.com'] });
    expect(result.ok && result.values.disabledSites).toEqual(['www.example.com', 'example.com']);
  });
});
