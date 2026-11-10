/** Sites and cookie scope, consent grants, the task tracker, and the task / consent message contract. */
import { get } from 'svelte/store';
import { describe, expect, it } from 'vitest';

import { isAllowed, parseConfigChange, parseSwRequest, parseTabMessage, type SenderKind } from '@/lib/messages';
import {
  type ChromeCookie, GRANT_TTL_MS, grantCovers, hostInSite, MAX_GRANTS, parseGrants, siteOf, taskSites,
  toCookieRecords,
} from '@/lib/task/sites';
import { TrackerController, trackerStore } from '@/lib/task/tracker';

const TASK = 'task00000001';

describe('sites', () => {
  it('works out registrable domains, private suffixes included, and refuses what is not a website', () => {
    expect(siteOf('https://www.amazon.co.uk/cart')).toBe('amazon.co.uk');
    expect(siteOf('http://shop.test/products')).toBe('shop.test');
    expect(siteOf('https://alice.github.io/')).toBe('alice.github.io');      // not all of github.io
    for (const url of ['https://co.uk/', 'http://127.0.0.1:8000/', 'http://localhost/', 'chrome://settings',
      'file:///C:/x.html', 'not a url', null]) {
      expect(siteOf(url)).toBeNull();
    }
    expect(hostInSite('.cdn.shop.test', 'shop.test')).toBe(true);
    expect(hostInSite('notshop.test', 'shop.test')).toBe(false);
  });

  it('scopes a task to the tab, and flags a start URL on another site as cross-site', () => {
    expect(taskSites('http://shop.test/p', null)).toEqual({ tabSite: 'shop.test', taskSite: 'shop.test', crossSite: false,
      sites: ['shop.test'] });
    expect(taskSites('http://shop.test/p', 'https://www.shop.test/cart')?.crossSite).toBe(false);
    expect(taskSites('http://shop.test/p', 'https://bank.example/pay')).toEqual({ tabSite: 'shop.test',
      taskSite: 'bank.example', crossSite: true, sites: ['shop.test', 'bank.example'] });
    expect(taskSites('chrome://newtab', null)).toBeNull();
    expect(taskSites('http://shop.test/', 'javascript:alert(1)')).toBeNull();
  });

  it('keeps only the site cookies and maps them to the wire form', () => {
    const base = { path: '/', httpOnly: false, secure: false, sameSite: 'unspecified', hostOnly: false, session: true };
    const cookies = [
      { ...base, name: 'session', value: 'a', domain: 'shop.test', httpOnly: true, sameSite: 'lax', hostOnly: true },
      { ...base, name: 'pref', value: 'b', domain: '.shop.test', session: false, expirationDate: 1_900_000_000 },
      { ...base, name: 'chips', value: 'c', domain: '.cdn.shop.test', secure: true, sameSite: 'no_restriction',
        partitionKey: { topLevelSite: 'https://shop.test' } },
      { ...base, name: 'other', value: 'x', domain: '.evil.example' },
      { ...base, name: 'session', value: 'a', domain: 'shop.test', httpOnly: true, sameSite: 'lax', hostOnly: true },
    ] as ChromeCookie[];
    const records = toCookieRecords(cookies, 'shop.test');
    expect(records.map((r) => r.name)).toEqual(['session', 'pref', 'chips']);         // other site, duplicate: gone
    expect(records[0]).toMatchObject({ host_only: true, http_only: true, same_site: 'Lax', expires: null });
    expect(records[1]).toMatchObject({ expires: 1_900_000_000, same_site: null });
    expect(records[2]).toMatchObject({ same_site: 'None', partition_key: 'https://shop.test' });
  });
});

describe('consent grants', () => {
  it('only covers a same-site task that asks for no more than was granted', () => {
    const grants = parseGrants({ 'shop.test': { session: true, at: 1 }, 'news.example': { session: false, at: 2 },
      'co.uk': { session: true, at: 3 }, 'shop.test.': { session: true, at: 4 }, bad: 'x' }, 10);
    expect(Object.keys(grants).sort()).toEqual(['news.example', 'shop.test']);   // never a public suffix
    expect(grantCovers(grants, taskSites('http://shop.test/', null)!, true)).toBe(true);
    expect(grantCovers(grants, taskSites('https://news.example/', null)!, false)).toBe(true);
    expect(grantCovers(grants, taskSites('https://news.example/', null)!, true)).toBe(false);  // no login granted
    expect(grantCovers(grants, taskSites('http://shop.test/', 'https://bank.example/')!, true)).toBe(false);
    expect(grantCovers(grants, taskSites('https://other.example/', null)!, false)).toBe(false);
  });

  it('lapse 90 days after their last use, refuse future dates, and keep the 50 most recently used', () => {
    const now = Date.UTC(2026, 10, 9);
    const day = 24 * 60 * 60 * 1000;
    const grants = parseGrants({
      'old.example': { session: true, at: now - 120 * day },                              // given, never used
      'kept.example': { session: true, at: now - 200 * day, used: now - 10 * day },       // still in use
      'future.example': { session: true, at: now + 30 * day },
    }, now);
    expect(Object.keys(grants)).toEqual(['kept.example']);
    expect(GRANT_TTL_MS).toBe(90 * day);

    const many = Object.fromEntries(Array.from({ length: 60 }, (_, i) => [`site${i}.example`, { session: false,
      at: now - i * 1000 }]));
    const capped = parseGrants(many, now);
    expect(Object.keys(capped)).toHaveLength(MAX_GRANTS);
    expect(capped['site0.example']).toBeDefined();                       // newest kept, oldest dropped
    expect(capped['site59.example']).toBeUndefined();
  });
});

describe('runtime config requests', () => {
  it('accept only safe options, a known capability and profile, and come only from extension pages', () => {
    expect(parseConfigChange({ capability: 'tts', choice: { provider: 'kokoro', options: { voice: 'af_bella' } } }))
      .toEqual({ capability: 'tts', choice: { provider: 'kokoro', options: { voice: 'af_bella' } } });
    expect(parseConfigChange({ profile: 'cloud' })).toEqual({ profile: 'cloud' });
    expect(parseConfigChange({ capability: 'tts', reset: true })).toEqual({ capability: 'tts', reset: true });
    for (const bad of [
      { capability: 'tts', choice: { provider: 'openai', options: { base_url: 'https://evil.example' } } },
      { capability: 'tts', choice: { provider: 'Kokoro!' } },
      { capability: 'tts', choice: { provider: 'kokoro', model: 'x'.repeat(201) } },
      { capability: 'weather', choice: { provider: 'fake' } },
      { profile: 'fake' },
      { capability: 'tts' },                                                // neither a choice nor a reset
      {},
    ]) {
      expect(parseConfigChange(bad)).toBeNull();
    }
    expect(parseSwRequest({ type: 'config.set', change: { profile: 'local' } })).not.toBeNull();
    for (const kind of ['content-top', 'content-frame', 'foreign'] as SenderKind[]) {
      expect(isAllowed('config.set', kind)).toBe(false);
      expect(isAllowed('config.get', kind)).toBe(false);
      expect(isAllowed('grants.clear', kind)).toBe(false);
    }
    expect(isAllowed('config.set', 'extension-page')).toBe(true);
    expect(parseTabMessage({ type: 'worker.started' })).toEqual({ type: 'worker.started' });
  });
});

describe('task tracker', () => {
  it('ends a running task when the service worker restarts, and leaves a finished one alone', () => {
    const tracker = new TrackerController({ stop: () => undefined, answer: () => undefined, open: () => undefined });
    tracker.onEvent({ type: 'task.consent', task_id: TASK, goal: 'Buy a mug', sites: ['shop.test'], cross_site: false,
      session: true, cookies: 1, auto: true });
    tracker.onEvent({ type: 'task.step', task_id: TASK, n: 1, action_text: 'Opening the shop' });
    tracker.workerRestarted();
    expect(get(trackerStore)).toMatchObject({ status: 'failed', confirm: null });
    expect(get(trackerStore).summary).toContain('connection to the backend was interrupted');
    const before = get(trackerStore);
    tracker.workerRestarted();
    expect(get(trackerStore)).toEqual(before);
    tracker.close();
  });

  it('follows a task from consent to done and only answers its own questions', () => {
    const sent: string[] = [];
    const tracker = new TrackerController({
      stop: (id) => sent.push(`stop ${id}`),
      answer: (id, request, approved) => sent.push(`answer ${id} ${request} ${approved}`),
      open: (id, url) => sent.push(`open ${id} ${url}`),
    });
    tracker.onEvent({ type: 'task.consent', task_id: TASK, goal: 'Buy a mug', sites: ['shop.test'], cross_site: false,
      session: true, cookies: 2, auto: false });
    expect(get(trackerStore)).toMatchObject({ open: true, status: 'consent', goal: 'Buy a mug', cookies: 2 });
    tracker.onEvent({ type: 'task.step', task_id: 'someone-else-1', n: 1, action_text: 'x' });     // not this task
    expect(get(trackerStore).steps).toEqual([]);
    tracker.onEvent({ type: 'task.started', task_id: TASK, goal: 'Buy a mug', sites: ['shop.test'], cookies: 2 });
    for (let n = 1; n <= 8; n += 1) tracker.onEvent({ type: 'task.step', task_id: TASK, n, action_text: `step ${n}` });
    expect(get(trackerStore).steps.map((s) => s.n)).toEqual([3, 4, 5, 6, 7, 8]);          // the last six
    tracker.onEvent({ type: 'task.confirm.request', task_id: TASK, request_id: 'r1', question: 'Pay $12?' });
    expect(get(trackerStore).status).toBe('confirm');
    tracker.answer(true);
    tracker.stop();
    tracker.onEvent({ type: 'task.done', task_id: TASK, success: true, summary: 'Ordered.', final_url: 'http://shop.test/o' });
    tracker.openInTab();
    tracker.stop();                                                           // finished: nothing to stop
    expect(sent).toEqual([`answer ${TASK} r1 true`, `stop ${TASK}`, `open ${TASK} http://shop.test/o`]);
    expect(get(trackerStore)).toMatchObject({ status: 'done', summary: 'Ordered.' });
    tracker.close();
    expect(get(trackerStore).open).toBe(false);
  });
});

describe('task messages', () => {
  const page: SenderKind = 'extension-page';
  const top: SenderKind = 'content-top';
  const frame: SenderKind = 'content-frame';

  it('parse strictly and go only to their senders', () => {
    expect(parseSwRequest({ type: 'consent.decide', taskId: TASK, decision: 'always' })).toEqual(
      { type: 'consent.decide', taskId: TASK, decision: 'always' });
    expect(parseSwRequest({ type: 'consent.decide', taskId: TASK, decision: 'maybe' })).toBeNull();
    expect(parseSwRequest({ type: 'task.answer', taskId: '<x>', requestId: 'r', approved: true })).toBeNull();
    expect(parseSwRequest({ type: 'task.open', taskId: TASK, url: 'http://shop.test/' })).not.toBeNull();
    expect(isAllowed('consent.decide', page)).toBe(true);
    expect(isAllowed('consent.decide', top)).toBe(false);                  // a page can never answer for the user
    expect(isAllowed('task.answer', top)).toBe(true);
    expect(isAllowed('task.answer', frame)).toBe(false);
    expect(isAllowed('grants.remove', top)).toBe(false);
    expect(parseTabMessage({ type: 'task.event', event: { type: 'task.done', task_id: TASK, success: true,
      summary: 'ok' } })).not.toBeNull();
    expect(parseTabMessage({ type: 'task.event', event: { type: 'task.done', task_id: TASK } })).toBeNull();
    expect(parseTabMessage({ type: 'task.event', event: { type: 'consent.request', task_id: TASK } })).toBeNull();
  });
});
