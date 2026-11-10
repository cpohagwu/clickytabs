/**
 * Browser tasks in the service worker: consent, the session bundle, and routing.
 *
 *   consent.request (backend)  -> sites from the REAL tab URL (chrome.tabs) and the start URL, never from the model;
 *                                 a page that isn't a website, or a start URL that isn't one, is refused at once
 *                              -> "Always" grant for this site and no more access? allowed without asking;
 *                                 otherwise an extension popup window (consent.html) - outside the page, so the page
 *                                 can't cover or click it
 *   consent.decide (window)    -> consent.response {decision, sites, tab_url}; then, if the login is shared,
 *                                 session.bundle: cookies of the tab's site only (+ partitioned ones), the tab
 *                                 origin's localStorage, user agent / language / time zone / viewport for parity
 *   task.* (backend)           -> the tab that asked; the tracker's stop / answer / open come back only from it
 *
 * Unanswered requests are refused after CONSENT_TIMEOUT_MS; closing the window is "no".
 * Service-worker only: it uses extension APIs (never imported by content scripts).
 */
import type { Browser } from 'wxt/browser';

import { createLogger } from '../log';
import type { ConsentDecision, ConsentView, TabMessage } from '../messages';
import type {
  ClientEvent, ConsentRequest, OriginStorage, TaskConfirmRequest, TaskDone, TaskStarted, TaskStep,
} from '../protocol.gen';
import {
  type ChromeCookie, grantCovers, GRANTS_KEY, type Grants, hostInSite, parseGrants, siteOf, type TaskSites,
  taskSites, toCookieRecords,
} from './sites';
import type { TaskConsentView } from './tracker';

const log = createLogger('task');

export const CONSENT_TIMEOUT_MS = 150_000;
const MAX_OWNERS = 32;
const MAX_STORAGE_ENTRIES = 500;
const MAX_STORAGE_CHARS = 1_000_000;

export interface TaskHubDeps {
  browser: typeof import('wxt/browser').browser;
  send: (event: ClientEvent) => boolean;
  toTab: <T>(tabId: number, message: TabMessage, allFrames?: boolean, timeoutMs?: number) => Promise<T | undefined>;
  tabForTurn: (turnId: string) => number | undefined;
}

interface Pending {
  request: ConsentRequest;
  tabId: number;
  tabUrl: string;
  tabTitle: string;
  sites: TaskSites;
  cookieCount: number;
  windowId: number | null;
  timer: ReturnType<typeof setTimeout>;
}

interface Owner {
  tabId: number;
  sites: string[];
}

export class TaskHub {
  private readonly pending = new Map<string, Pending>();
  private readonly owners = new Map<string, Owner>();

  constructor(private readonly deps: TaskHubDeps) {}

  // ------------------------------------------------------------------------------------------------ consent

  async onConsentRequest(request: ConsentRequest): Promise<void> {
    const tabId = this.deps.tabForTurn(request.turn_id);
    const tab = tabId === undefined ? null : await this.deps.browser.tabs.get(tabId).catch(() => null);
    const sites = taskSites(tab?.url, request.start_url ?? null);
    if (tabId === undefined || !tab?.url || !sites) {
      log.warn('refusing a task: no website tab (or start URL) to scope it to');
      this.respond(request.task_id, 'deny', [], null);
      return;
    }
    this.own(request.task_id, { tabId, sites: sites.sites });
    const cookieCount = request.session ? (await this.cookies(sites.tabSite, tab.url)).length : 0;
    const grants = await this.grants();
    const auto = grantCovers(grants, sites, request.session ?? true);
    if (auto) {                                    // keeps a grant in use from lapsing
      grants[sites.tabSite] = { ...grants[sites.tabSite]!, used: Date.now() };
      await this.deps.browser.storage.local.set({ [GRANTS_KEY]: grants });
    }
    const consentView: TaskConsentView = {
      type: 'task.consent', task_id: request.task_id, goal: request.goal, sites: sites.sites,
      cross_site: sites.crossSite, session: request.session ?? true, cookies: cookieCount, auto,
    };
    void this.deps.toTab(tabId, { type: 'task.event', event: consentView });
    const pending: Pending = {
      request, tabId, tabUrl: tab.url, tabTitle: (tab.title ?? '').slice(0, 120), sites, cookieCount, windowId: null,
      timer: setTimeout(() => void this.expire(request.task_id), CONSENT_TIMEOUT_MS),
    };
    this.pending.set(request.task_id, pending);
    if (auto) {
      await this.settle(pending, 'allow_once');
      return;
    }
    try {
      const window = await this.deps.browser.windows.create({
        url: this.deps.browser.runtime.getURL(`/consent.html?task=${encodeURIComponent(request.task_id)}`),
        type: 'popup', width: 520, height: 600, focused: true,
      });
      pending.windowId = window?.id ?? null;
    } catch (error) {
      log.warn('could not open the consent window', error);
      await this.settle(pending, 'deny');
    }
  }

  /** The consent window asks what it is about; only the window opened for that task gets an answer. */
  view(taskId: string, sender: Browser.runtime.MessageSender): ConsentView | { ok: false; error: string } {
    const pending = this.pending.get(taskId);
    if (!pending || !this.isConsentWindow(pending, sender)) return { ok: false, error: 'This request is no longer open.' };
    return {
      taskId, goal: pending.request.goal, tabSite: pending.sites.tabSite, taskSite: pending.sites.taskSite,
      crossSite: pending.sites.crossSite, session: pending.request.session ?? true, cookieCount: pending.cookieCount,
      startUrl: pending.request.start_url ?? null, tabTitle: pending.tabTitle,
    };
  }

  async decide(taskId: string, decision: ConsentDecision, sender: Browser.runtime.MessageSender): Promise<boolean> {
    const pending = this.pending.get(taskId);
    if (!pending || !this.isConsentWindow(pending, sender)) return false;
    // "Always" is never available for a cross-site task, whatever the window sent.
    await this.settle(pending, decision === 'always' && pending.sites.crossSite ? 'allow_once' : decision);
    return true;
  }

  /** The user closed the consent window without answering: no. */
  onWindowRemoved(windowId: number): void {
    for (const pending of this.pending.values()) {
      if (pending.windowId === windowId) void this.settle(pending, 'deny', false);
    }
  }

  private isConsentWindow(pending: Pending, sender: Browser.runtime.MessageSender): boolean {
    const page = this.deps.browser.runtime.getURL('/consent.html');
    return pending.windowId !== null && sender.tab?.windowId === pending.windowId
      && typeof sender.url === 'string' && sender.url.startsWith(page);
  }

  private async expire(taskId: string): Promise<void> {
    const pending = this.pending.get(taskId);
    if (pending) await this.settle(pending, 'deny');
  }

  private async settle(pending: Pending, decision: ConsentDecision, closeWindow = true): Promise<void> {
    const taskId = pending.request.task_id;
    if (this.pending.get(taskId) !== pending) return;            // answered already
    this.pending.delete(taskId);
    clearTimeout(pending.timer);
    if (closeWindow && pending.windowId !== null) {
      void this.deps.browser.windows.remove(pending.windowId).catch(() => undefined);
    }
    if (decision === 'deny') {
      this.respond(taskId, 'deny', [], null);
      return;
    }
    if (decision === 'always' && !pending.sites.crossSite) {
      const grants = await this.grants();
      grants[pending.sites.tabSite] = { session: pending.request.session ?? true, at: Date.now() };
      await this.deps.browser.storage.local.set({ [GRANTS_KEY]: grants });
    }
    this.respond(taskId, decision, pending.sites.sites, pending.tabUrl);
    if (pending.request.session ?? true) await this.sendBundle(pending);
  }

  private respond(taskId: string, decision: ConsentDecision, sites: string[], tabUrl: string | null): void {
    this.deps.send({ type: 'consent.response', task_id: taskId, decision, sites, tab_url: tabUrl });
  }

  // ------------------------------------------------------------------------------------------------ session

  private async cookies(site: string, tabUrl: string) {
    const cookies = this.deps.browser.cookies;
    const plain = await cookies.getAll({ domain: site }).catch(() => []);
    let partitioned: unknown[] = [];
    try {
      // CHIPS cookies of embeds on this site's pages (Chrome 119+)
      const topLevelSite = `${new URL(tabUrl).protocol}//${site}`;
      partitioned = await cookies.getAll({ domain: site, partitionKey: { topLevelSite } } as never);
    } catch {
      partitioned = [];
    }
    return toCookieRecords([...plain, ...partitioned] as ChromeCookie[], site);
  }

  private async sendBundle(pending: Pending): Promise<void> {
    const site = pending.sites.tabSite;
    const cookies = await this.cookies(site, pending.tabUrl);
    const origins: OriginStorage[] = [];
    const storage = await this.deps.toTab<{ origin: string; entries: Record<string, string> } | null>(
      pending.tabId, { type: 'context.storage' });
    if (storage && typeof storage.origin === 'string' && siteOf(storage.origin) === site
      && typeof storage.entries === 'object' && storage.entries !== null) {
      const entries: Record<string, string> = {};
      let chars = 0;
      for (const [key, value] of Object.entries(storage.entries).slice(0, MAX_STORAGE_ENTRIES)) {
        if (typeof value !== 'string') continue;
        chars += key.length + value.length;
        if (chars > MAX_STORAGE_CHARS) break;
        entries[key] = value;
      }
      origins.push({ origin: storage.origin, local_storage: entries });
    }
    const tab = await this.deps.browser.tabs.get(pending.tabId).catch(() => null);
    this.deps.send({
      type: 'session.bundle', task_id: pending.request.task_id, site, cookies, origins,
      user_agent: navigator.userAgent, locale: navigator.language,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      viewport: tab?.width && tab.height ? [tab.width, tab.height] : null,
    });
  }

  // ------------------------------------------------------------------------------------------------ routing

  private own(taskId: string, owner: Owner): void {
    this.owners.set(taskId, owner);
    while (this.owners.size > MAX_OWNERS) this.owners.delete(this.owners.keys().next().value!);
  }

  route(event: TaskStarted | TaskStep | TaskConfirmRequest | TaskDone): void {
    const owner = this.owners.get(event.task_id);
    if (!owner) return;
    void this.deps.toTab(owner.tabId, { type: 'task.event', event });
  }

  /** The tracker's buttons: accepted only from the tab the task belongs to. */
  stop(taskId: string, tabId: number | undefined): boolean {
    if (tabId === undefined || this.owners.get(taskId)?.tabId !== tabId) return false;
    const pending = this.pending.get(taskId);
    if (pending) void this.settle(pending, 'deny');
    else this.deps.send({ type: 'task.cancel', task_id: taskId });
    return true;
  }

  answer(taskId: string, requestId: string, approved: boolean, tabId: number | undefined): boolean {
    if (tabId === undefined || this.owners.get(taskId)?.tabId !== tabId) return false;
    return this.deps.send({ type: 'task.confirm.response', task_id: taskId, request_id: requestId, approved });
  }

  async open(taskId: string, url: string, tabId: number | undefined): Promise<boolean> {
    const owner = this.owners.get(taskId);
    if (tabId === undefined || owner?.tabId !== tabId) return false;
    let parsed: URL;
    try {
      parsed = new URL(url);
    } catch {
      return false;
    }
    if ((parsed.protocol !== 'https:' && parsed.protocol !== 'http:')
      || !owner.sites.some((site) => hostInSite(parsed.hostname, site))) {
      return false;                                              // only somewhere the task was allowed to go
    }
    await this.deps.browser.tabs.update(tabId, { url: parsed.href });
    return true;
  }

  // ------------------------------------------------------------------------------------------------ grants

  async grants(): Promise<Grants> {
    const stored = await this.deps.browser.storage.local.get(GRANTS_KEY);
    return parseGrants(stored[GRANTS_KEY]);
  }

  async removeGrant(site: string): Promise<void> {
    const grants = await this.grants();
    delete grants[site];
    await this.deps.browser.storage.local.set({ [GRANTS_KEY]: grants });
  }

  /** Forget every grant (options "Forget all", or pairing with another backend). */
  async clearGrants(): Promise<void> {
    await this.deps.browser.storage.local.remove(GRANTS_KEY);
  }
}
