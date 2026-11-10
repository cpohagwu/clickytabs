/**
 * Service worker: the extension's hub.
 *
 * - Owns the single backend WebSocket (lib/socket.ts): connects at startup, on install, on any message, when the
 *   pairing settings change, and from a 30 s alarm while disconnected (timers die with the worker; alarms don't).
 * - Locks chrome.storage.local to trusted contexts, so content scripts can never read the token or redirect the
 *   backend; content scripts get only what the sender rules in lib/messages.ts allow.
 * - Owns push-to-talk (lib/ptt.ts): frames report raw chord events; browser events release stuck holds.
 * - Relays debug text turns from the popup (port "debug-turn") and HUD text turns from a tab's companion
 *   (`turn.text`) to the backend, and routes each turn's events back to whoever started it.
 * - Page context: a HUD turn carries the tab's DOM snapshot (from its content script) and a screenshot, scaled to
 *   the budget the backend announced for the active brain, with element boxes mapped into screenshot pixels.
 * - Voice turns: push-to-talk starts the offscreen recorder and captures the page in parallel; audio chunks are
 *   buffered until `turn.start` (with the page) is out, then streamed; key release sends `audio.end`. Speech comes
 *   back as segments that go to the offscreen player in order; playback progress goes to the tab (captions, pointer
 *   timing) and back to the backend. Pressing the key while it talks stops the answer at once (barge-in).
 */
import { type Browser, browser } from 'wxt/browser';
import { defineBackground } from 'wxt/utils/define-background';

import { APP_NAME } from '@/lib/brand';
import { CONN_KEY, type ConnInfo, type ConnState, needsPairing } from '@/lib/conn';
import { createLogger } from '@/lib/log';
import {
  type Ack,
  type CaptureReport,
  classifySender,
  type ConfigChange,
  DEBUG_TURN_PORT,
  type DebugStateReply,
  type DebugTurnEvent,
  isAllowed,
  isSceneId,
  MIC_KEY,
  type MicInfo,
  OFFSCREEN_LIFELINE_PORT,
  type MicProbeResult,
  type OffscreenPingReply,
  type OffscreenRequest,
  type OrbState,
  parseDebugTurnCommand,
  type RecStartReply,
  type VoiceTurnRecord,
  parseSwRequest,
  type PublicStatus,
  type ScenePingReply,
  type SenderLike,
  type SwRequest,
  type SwResponseMap,
  type TabMessage,
  TAB_TURN_EVENT_TYPES,
  type TabTurnEvent,
} from '@/lib/messages';
import { ensureOffscreen } from '@/lib/offscreen';
import { TaskHub } from '@/lib/task/hub';
import {
  boxToImage,
  bytesToBase64,
  colorsMatch,
  FIELD_MASK,
  dataUrlToBlob,
  DEFAULT_PAGE_BUDGET,
  NAMED_PAGE_BUDGETS,
  fitWithin,
  type Rgb,
  SENTINEL_CSS_PX,
  sentinelProbePoints,
  toImageSpace,
} from '@/lib/page-context/capture';
import type { PageSnapshot, SnapshotBudget } from '@/lib/page-context/snapshot';
import { isSiteDisabled } from '@/lib/prefs';
import type {
  ClientEvent, ConfigState, PageBudget, PageContext, SceneBuilding, SceneFailed, SceneRender, Screenshot, ServerEvent,
} from '@/lib/protocol.gen';
import { PttController, type PttReleaseReason } from '@/lib/ptt';
import {
  CONNECTION_KEYS,
  loadSettings,
  PREF_KEYS,
  saveSettings,
  toPagePrefs,
  validateSettingsPatch,
} from '@/lib/settings';
import {
  CONVERSATION_KEY,
  RECONNECT_ALARM,
  RECONNECT_ALARM_PERIOD_MIN,
  SocketManager,
  wantsReconnectAlarm,
} from '@/lib/socket';

const log = createLogger('sw');
const BLOCKED_KEY = 'blockedCredentials';
const HEALTH_PROBE_TIMEOUT_MS = 3_000;
const TAB_REPLY_TIMEOUT_MS = 5_000;
const SCENE_DEBUG_TIMEOUT_MS = 30_000;     // debug.sceneRender waits for the sandbox's verdict
const CONFIG_TIMEOUT_MS = 45_000;          // config.set health-checks the new provider (up to 10 s each)
/** chrome.tabs.captureVisibleTab allows at most 2 calls per second. */
const CAPTURE_INTERVAL_MS = 520;
const CAPTURE_ATTEMPTS = 3;

export default defineBackground(() => {
  const extensionId = browser.runtime.id;
  const orbStates = new Map<number, OrbState>();
  const debugTurns = new Map<string, Browser.runtime.Port>();
  /** HUD turns: turn id -> the tab whose companion started it. */
  const tabTurns = new Map<string, number>();
  /** Scenes: scene id -> the tab whose stage shows it (scene events outlive their turn). */
  const sceneTabs = new Map<string, number>();
  const MAX_SCENE_TABS = 32;

  // ------------------------------------------------------------------------------------------- storage lockdown

  const lockStorage = (async () => {
    try {
      await browser.storage.local.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' });
    } catch (error) {
      log.error('could not restrict chrome.storage.local to trusted contexts', error);
    }
  })();

  // ------------------------------------------------------------------------------------------- broadcasting

  /** Top frame only, unless `allFrames` (the push-to-talk script runs in every frame). */
  async function toTab<T>(tabId: number, message: TabMessage, allFrames = false,
    timeoutMs = TAB_REPLY_TIMEOUT_MS): Promise<T | undefined> {
    try {
      const sent = allFrames ? browser.tabs.sendMessage(tabId, message) : browser.tabs.sendMessage(tabId, message, { frameId: 0 });
      return (await withTimeout(sent, timeoutMs)) as T;
    } catch {
      return undefined; // no content script there (chrome://, Web Store, PDF viewer, not injected yet)
    }
  }

  async function broadcast(message: TabMessage, allFrames = false): Promise<void> {
    const tabs = await browser.tabs.query({});
    await Promise.all(tabs.map((tab) => (tab.id === undefined ? undefined : toTab(tab.id, message, allFrames))));
  }

  function publicStatus(tabId: number | undefined): PublicStatus {
    return { state: manager.conn.state, orb: (tabId !== undefined && orbStates.get(tabId)) || 'idle' };
  }

  function setOrb(tabId: number, state: OrbState): void {
    if (state === 'idle') orbStates.delete(tabId);
    else orbStates.set(tabId, state);
    void toTab(tabId, { type: 'orb.state', state });
  }

  async function syncBadge(state: ConnState): Promise<void> {
    const attention = needsPairing(state);
    await browser.action.setBadgeBackgroundColor({ color: attention ? '#f5a524' : '#7c5cff' });
    await browser.action.setBadgeText({ text: attention ? '!' : '' });
    await browser.action.setTitle({ title: `${APP_NAME}: ${state}` });
  }

  async function syncReconnectAlarm(state: ConnState): Promise<void> {
    const existing = await browser.alarms.get(RECONNECT_ALARM);
    if (wantsReconnectAlarm(state)) {
      if (!existing) await browser.alarms.create(RECONNECT_ALARM, { periodInMinutes: RECONNECT_ALARM_PERIOD_MIN });
    } else if (existing) {
      await browser.alarms.clear(RECONNECT_ALARM);
    }
  }

  // ------------------------------------------------------------------------------------------- backend socket

  /** config.get / config.set from the options page, waiting for the backend's config.state. */
  const configWaiters = new Map<string, (state: ConfigState) => void>();

  async function askConfig(change: ConfigChange | null): Promise<ConfigState | { ok: false; error: string }> {
    if (!manager.isOpen) {
      void manager.ensureConnected('options');
      return { ok: false, error: `Not connected to the backend (${manager.conn.state}).` };
    }
    const requestId = crypto.randomUUID();
    const reply = new Promise<ConfigState>((resolve) => configWaiters.set(requestId, resolve));
    const sent = change === null
      ? manager.send({ type: 'config.get', request_id: requestId })
      : manager.send({ type: 'config.set', request_id: requestId, ...change });
    if (!sent) {
      configWaiters.delete(requestId);
      return { ok: false, error: 'The connection dropped; try again.' };
    }
    try {
      return await withTimeout(reply, CONFIG_TIMEOUT_MS);
    } catch {
      return { ok: false, error: 'The backend did not answer in time.' };
    } finally {
      configWaiters.delete(requestId);
    }
  }

  function routeServerEvent(event: ServerEvent): void {
    if (event.type === 'config.state') {                // the socket manager already applied the page budget
      if (event.request_id) configWaiters.get(event.request_id)?.(event);
      return;
    }
    if (event.type === 'conversation.started') {        // after conversation.reset: continue this one from now on
      void browser.storage.session.set({ [CONVERSATION_KEY]: event.conversation_id });
      return;
    }
    if (event.type === 'scene.building' || event.type === 'scene.render' || event.type === 'scene.failed') {
      routeSceneEvent(event);
      return;
    }
    if (event.type === 'consent.request') {             // a browser task the brain proposed
      void tasks.onConsentRequest(event);
      return;
    }
    if (event.type === 'task.started' || event.type === 'task.step' || event.type === 'task.confirm.request'
      || event.type === 'task.done') {
      tasks.route(event);
      return;
    }
    const turnId = 'turn_id' in event ? event.turn_id : undefined;
    if (typeof turnId !== 'string') return;
    if (routeVoiceEvent(turnId, event)) return;      // speech audio goes to the player, not the tab
    const tabId = tabTurns.get(turnId);
    if (tabId !== undefined) {
      if (isTabTurnEvent(event)) void toTab(tabId, { type: 'turn.event', event });
      if (event.type === 'turn.done') tabTurns.delete(turnId);
      return;
    }
    const port = debugTurns.get(turnId);
    if (!port) return;
    if (event.type === 'turn.state' || event.type === 'assistant.text.delta' || event.type === 'turn.done'
      || event.type === 'error') {
      safePost(port, event satisfies DebugTurnEvent);
    }
    if (event.type === 'turn.done') debugTurns.delete(turnId);
  }

  /** Scene events go to the tab whose turn asked for the scene; a render nobody can show is answered at once. */
  function routeSceneEvent(event: SceneBuilding | SceneRender | SceneFailed): void {
    let tabId = sceneTabs.get(event.scene_id);
    if (tabId === undefined && event.type === 'scene.building') {
      tabId = tabTurns.get(event.turn_id) ?? voiceRecord(event.turn_id)?.tabId;
      if (tabId !== undefined) {
        sceneTabs.set(event.scene_id, tabId);
        while (sceneTabs.size > MAX_SCENE_TABS) sceneTabs.delete(sceneTabs.keys().next().value!);
      }
    }
    const unavailable = (error: string): void => {
      if (event.type === 'scene.render') {
        manager.send({ type: 'scene.result', scene_id: event.scene_id, attempt: event.attempt ?? 1, ok: false,
          unavailable: true, error });
      }
    };
    if (tabId === undefined) {
      unavailable('no tab to show the scene in');
      return;
    }
    void toTab<unknown>(tabId, { type: 'scene.event', event }).then((reply) => {
      if (reply === undefined) unavailable('the tab is gone or has no companion');
    });
    if (event.type === 'scene.failed') sceneTabs.delete(event.scene_id);
  }

  function failPendingTurns(detail: string): void {
    if (recordingTurn) toOffscreenInOrder({ target: 'offscreen', type: 'rec.cancel' });
    recordingTurn = null;
    toOffscreenInOrder({ target: 'offscreen', type: 'play.stop' });
    liveVoice.clear();
    for (const [turnId, port] of debugTurns) {
      safePost(port, { type: 'turn.done', turn_id: turnId, reason: 'error', detail });
    }
    debugTurns.clear();
    for (const [turnId, tabId] of tabTurns) {
      void toTab(tabId, { type: 'turn.event', event: { type: 'turn.done', turn_id: turnId, reason: 'error', detail } });
    }
    tabTurns.clear();
  }

  async function startTabTurn(tabId: number, text: string): Promise<SwResponseMap['turn.text']> {
    if (!manager.isOpen) {
      void manager.ensureConnected('hud-turn');
      return { ok: false, error: `Not connected to the backend (${manager.conn.state}).` };
    }
    cancelTabTurns(tabId); // one turn per tab: a new question replaces the old one
    const turnId = crypto.randomUUID();
    tabTurns.set(turnId, tabId);
    const context = await collectPageContext(tabId).catch((error: unknown) => {
      log.warn('page context failed; sending the question alone', error);
      return null;
    });
    if (!tabTurns.has(turnId)) return { ok: false, error: 'Stopped.' }; // Stop pressed while capturing
    const page = context?.page ?? null;
    if (!manager.send({ type: 'turn.start', turn_id: turnId, mode: 'text', text, tab_id: tabId, page })) {
      tabTurns.delete(turnId);
      return { ok: false, error: 'The connection dropped; try again.' };
    }
    return { ok: true, turn_id: turnId };
  }

  // ------------------------------------------------------------------------------------------- page context

  function snapshotBudget(budget: PageBudget): SnapshotBudget {
    return { maxElements: budget.max_elements, maxTextChars: budget.max_text_chars, maxNameChars: budget.max_name_chars };
  }

  let lastCaptureAt = 0;

  function pixelAt(bitmap: ImageBitmap, [x, y]: [number, number]): Rgb {
    const canvas = new OffscreenCanvas(1, 1);
    const ctx = canvas.getContext('2d')!;
    ctx.drawImage(bitmap, Math.min(x, bitmap.width - 1), Math.min(y, bitmap.height - 1), 1, 1, 0, 0, 1, 1);
    const [r = 0, g = 0, b = 0] = ctx.getImageData(0, 0, 1, 1).data;
    return [r, g, b];
  }

  /**
   * The tab's window as the user sees it, scaled down to the budget and re-encoded as JPEG. A capture that doesn't
   * show the sentinel predates the companion hiding itself (stale frame): it is retried, then given up (null).
   */
  async function captureScreenshot(windowId: number, budget: PageBudget, snapshot: PageSnapshot,
    report: CaptureReport): Promise<Screenshot | null> {
    const { viewport, sentinel } = snapshot;
    for (let attempt = 0; attempt < CAPTURE_ATTEMPTS; attempt += 1) {
      report.attempts += 1;
      const wait = lastCaptureAt + CAPTURE_INTERVAL_MS - Date.now();
      if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
      lastCaptureAt = Date.now();
      const dataUrl = await browser.tabs.captureVisibleTab(windowId, { format: 'jpeg', quality: 92 });
      const bitmap = await createImageBitmap(dataUrlToBlob(dataUrl));
      // The capture's own device-px per CSS px (not devicePixelRatio: emulation and zoom can differ from it).
      const probes = sentinelProbePoints(bitmap.width / viewport.width);
      try {
        if (sentinel && !colorsMatch(pixelAt(bitmap, probes.inside), sentinel)) {
          log.info('stale capture (no sentinel yet)', { attempt });
          report.stale += 1;
          continue;
        }
        const size = fitWithin(bitmap.width, bitmap.height, budget.image_long_edge, budget.image_max_pixels);
        const canvas = new OffscreenCanvas(size.width, size.height);
        const ctx = canvas.getContext('2d');
        if (!ctx) throw new Error('2D canvas unavailable');
        ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(bitmap, 0, 0, size.width, size.height);
        if (sentinel) {
          // Paint the sentinel over with the page pixel beside it.
          ctx.fillStyle = `rgb(${pixelAt(bitmap, probes.beside).join(' ')})`;
          const scale = size.width / viewport.width;
          ctx.fillRect(0, 0, Math.ceil(SENTINEL_CSS_PX * scale) + 1, Math.ceil(SENTINEL_CSS_PX * scale) + 1);
        }
        // Whatever the user typed stays out of the picture, as it stays out of the DOM payload.
        ctx.fillStyle = FIELD_MASK.fill;
        ctx.strokeStyle = FIELD_MASK.stroke;
        ctx.lineWidth = 1;
        for (const box of snapshot.field_boxes ?? []) {
          const [x, y, w, h] = boxToImage(box, viewport, size);
          ctx.fillRect(x, y, w, h);
          ctx.strokeRect(x + 0.5, y + 0.5, Math.max(0, w - 1), Math.max(0, h - 1));
        }
        const blob = await canvas.convertToBlob({ type: 'image/jpeg', quality: budget.jpeg_quality });
        const data = bytesToBase64(new Uint8Array(await blob.arrayBuffer()));
        return { data_b64: data, mime: 'image/jpeg', width: size.width, height: size.height };
      } finally {
        bitmap.close();
      }
    }
    log.warn('no fresh capture after retries; sending the DOM only');
    return null;
  }

  /**
   * DOM snapshot from the tab's content script, plus a screenshot when the brain has vision and the tab is the one
   * showing in its window (captureVisibleTab can only see that). The companion hides itself for the capture.
   */
  async function collectPageContext(tabId: number, budgetOverride?: PageBudget):
    Promise<{ page: PageContext; snapshotMs: number; capture: CaptureReport; phases: Record<string, number> } | null> {
    const budget = budgetOverride ?? manager.conn.pageBudget ?? DEFAULT_PAGE_BUDGET;
    const tab = await browser.tabs.get(tabId).catch(() => null);
    if (!tab) return null;
    const wantScreenshot = budget.vision && tab.active;
    const snapshot = await toTab<PageSnapshot>(tabId, {
      type: 'context.snapshot',
      budget: snapshotBudget(budget),
      hideUi: wantScreenshot,
    });
    if (!snapshot || !(snapshot.viewport?.width >= 1) || !(snapshot.viewport?.height >= 1)) return null;
    let screenshot: Screenshot | null = null;
    const capture: CaptureReport = { attempts: 0, stale: 0 };
    if (wantScreenshot) {
      try {
        screenshot = await captureScreenshot(tab.windowId, budget, snapshot, capture);
      } catch (error) {
        capture.error = error instanceof Error ? error.message : String(error);
        log.warn('screenshot failed; sending the DOM only', error);
      } finally {
        const image = screenshot ? { width: screenshot.width, height: screenshot.height } : null;
        void toTab(tabId, { type: 'context.restore', image });
      }
    }
    const page: PageContext = {
      url: snapshot.url,
      title: snapshot.title,
      viewport: snapshot.viewport,
      screenshot,
      elements: toImageSpace(snapshot.elements, snapshot.viewport, screenshot),
      selection: snapshot.selection,
      text_excerpt: snapshot.text_excerpt,
    };
    if (snapshot.scene && isSceneId(snapshot.scene.scene_id) && typeof snapshot.scene.title === 'string') {
      page.scene = { scene_id: snapshot.scene.scene_id, title: snapshot.scene.title.slice(0, 200) };
    }
    return { page, snapshotMs: snapshot.took_ms, capture, phases: snapshot.phases ?? {} };
  }

  function cancelTabTurns(tabId: number): void {
    for (const [turnId, owner] of tabTurns) {
      if (owner !== tabId) continue;
      tabTurns.delete(turnId);
      manager.send({ type: 'turn.cancel', turn_id: turnId });
    }
  }

  const tasks = new TaskHub({
    browser,
    send: (event) => manager.send(event),
    toTab: (tabId, message, allFrames, timeoutMs) => toTab(tabId, message, allFrames, timeoutMs),
    tabForTurn: (turnId) => tabTurns.get(turnId) ?? voiceRecord(turnId)?.tabId,
  });

  const manager = new SocketManager({
    readCredentials: async () => {
      await lockStorage;
      const settings = await loadSettings();
      return { backendUrl: settings.backendUrl, token: settings.token };
    },
    createSocket: (url, handlers) => {
      const socket = new WebSocket(url);
      socket.onopen = () => handlers.onOpen();
      socket.onmessage = (event) => {
        if (typeof event.data === 'string') handlers.onMessage(event.data);
      };
      socket.onclose = (event) => handlers.onClose(event.code, event.reason);
      return socket;
    },
    probeHealth: async (url) => {
      try {
        const response = await fetch(url, { cache: 'no-store', signal: AbortSignal.timeout(HEALTH_PROBE_TIMEOUT_MS) });
        return response.ok;
      } catch {
        return false;
      }
    },
    publish: (info: ConnInfo) => {
      void browser.storage.session.set({ [CONN_KEY]: info });
      void syncReconnectAlarm(info.state);
      void syncBadge(info.state);
      void broadcastStatus();
    },
    readBlocked: async () => {
      const stored = await browser.storage.local.get(BLOCKED_KEY);
      return typeof stored[BLOCKED_KEY] === 'string' ? stored[BLOCKED_KEY] : null;
    },
    writeBlocked: async (fingerprint) => {
      if (fingerprint) await browser.storage.local.set({ [BLOCKED_KEY]: fingerprint });
      else await browser.storage.local.remove(BLOCKED_KEY);
    },
    onServerEvent: routeServerEvent,
    onSessionEnded: () => failPendingTurns('The connection to the backend was lost.'),
    conversation: {
      read: async () => {
        const stored = await browser.storage.session.get(CONVERSATION_KEY);
        return typeof stored[CONVERSATION_KEY] === 'string' ? stored[CONVERSATION_KEY] : null;
      },
      write: (id) => void browser.storage.session.set({ [CONVERSATION_KEY]: id }),
    },
    extensionId,
    clientVersion: browser.runtime.getManifest().version,
    log: createLogger('socket'),
  });

  async function broadcastStatus(): Promise<void> {
    const tabs = await browser.tabs.query({});
    await Promise.all(tabs.map((tab) =>
      tab.id === undefined ? undefined : toTab(tab.id, { type: 'status.changed', status: publicStatus(tab.id) })));
  }

  // ------------------------------------------------------------------------------------------- push-to-talk

  const ptt = new PttController({
    started: (tabId) => {
      setOrb(tabId, 'listening');
      void toTab(tabId, { type: 'ptt.state', holding: true });
      void browser.action.setBadgeBackgroundColor({ tabId, color: '#ff2bd6' });
      void browser.action.setBadgeText({ tabId, text: 'REC' });
      void startVoiceTurn(tabId);
    },
    stopped: (tabId, reason: PttReleaseReason, heldMs) => {
      setOrb(tabId, 'idle');
      void toTab(tabId, { type: 'ptt.state', holding: false });
      void browser.action.setBadgeText({ tabId, text: '' });
      stopVoiceTurn(tabId, reason);
      log.info('push-to-talk ended', { tabId, reason, heldMs });
    },
  });

  // ------------------------------------------------------------------------------------------- voice turns

  interface LiveVoice {
    record: VoiceTurnRecord;
    sent: boolean;                 // turn.start is out: chunks go straight to the socket
    pending: ClientEvent[];        // chunks recorded while the page was being captured
    recDone: boolean;
    endSent: boolean;
    cancelled: boolean;
  }

  const MAX_VOICE_RECORDS = 20;
  const voiceRecords: VoiceTurnRecord[] = [];
  const liveVoice = new Map<string, LiveVoice>();
  let recordingTurn: string | null = null;
  let speakingTab: number | null = null;
  let speakingTurn: string | null = null;
  let offscreenQueue: Promise<unknown> = Promise.resolve();

  /**
   * Recorder/player commands reach the offscreen document strictly in order (audio chunks above all, and a quick
   * tap's rec.stop never overtakes its rec.start).
   */
  function toOffscreenQueued<T>(request: OffscreenRequest): Promise<T> {
    const reply = offscreenQueue.then(() => toOffscreen<T>(request));
    offscreenQueue = reply.catch((error: unknown) => log.warn(`offscreen ${request.type} failed`, error));
    return reply;
  }

  function toOffscreenInOrder(request: OffscreenRequest): void {
    void toOffscreenQueued(request).catch(() => undefined);
  }

  function voiceRecord(turnId: string): VoiceTurnRecord | undefined {
    return voiceRecords.find((record) => record.turnId === turnId);
  }

  async function openMicSetup(): Promise<void> {
    const url = browser.runtime.getURL('/onboarding.html');
    const [open] = await browser.tabs.query({ url: `${url}*` });
    if (open?.id !== undefined) await browser.tabs.update(open.id, { active: true, url: `${url}#mic` });
    else await browser.tabs.create({ url: `${url}#mic` });
  }

  function abortVoice(live: LiveVoice, message: string): void {
    live.cancelled = true;
    live.record.outcome = 'aborted';
    live.record.error = message;
    liveVoice.delete(live.record.turnId);
    tabTurns.delete(live.record.turnId);
    if (recordingTurn === live.record.turnId) recordingTurn = null;
    void toTab(live.record.tabId, { type: 'voice.error', message });
  }

  function sendAudioEnd(live: LiveVoice): void {
    if (live.endSent) return;
    live.endSent = true;
    manager.send({ type: 'audio.end', turn_id: live.record.turnId });
  }

  async function startVoiceTurn(tabId: number): Promise<void> {
    toOffscreenInOrder({ target: 'offscreen', type: 'play.stop' });   // barge-in: silence the answer first
    speakingTab = null;
    cancelTabTurns(tabId);
    if (!manager.isOpen) {
      void manager.ensureConnected('push-to-talk');
      void toTab(tabId, { type: 'voice.error', message: `Not connected to the backend (${manager.conn.state}).` });
      return;
    }
    const record: VoiceTurnRecord = { turnId: crypto.randomUUID(), tabId, pressedAt: Date.now(), chunks: 0 };
    voiceRecords.push(record);
    if (voiceRecords.length > MAX_VOICE_RECORDS) voiceRecords.shift();
    const live: LiveVoice = { record, sent: false, pending: [], recDone: false, endSent: false, cancelled: false };
    liveVoice.set(record.turnId, live);
    recordingTurn = record.turnId;
    tabTurns.set(record.turnId, tabId);
    void toTab(tabId, { type: 'voice.turn', turnId: record.turnId });

    // The microphone and the page capture start together; the backend's speech models warm up meanwhile.
    const [mic, context] = await Promise.all([
      toOffscreenQueued<RecStartReply>({ target: 'offscreen', type: 'rec.start', turnId: record.turnId })
        .catch((error: unknown): RecStartReply => ({ ok: false, name: 'Error', error: String(error) })),
      collectPageContext(tabId).catch(() => null),
    ]);
    if (live.cancelled) return;
    if (!mic.ok) {
      const needsGrant = mic.name === 'NotAllowedError';
      abortVoice(live, needsGrant ? 'Microphone access is needed: allow it on the setup page that just opened.'
        : `The microphone is unavailable (${mic.name}).`);
      await browser.storage.session.set({
        [MIC_KEY]: { state: needsGrant ? 'needs-grant' : 'unavailable', error: mic.name, updatedAt: Date.now() },
      });
      if (needsGrant) void openMicSetup();
      return;
    }
    record.micStartMs = mic.startMs;
    const page = context?.page ?? null;
    if (!manager.send({ type: 'turn.start', turn_id: record.turnId, mode: 'voice', tab_id: tabId, page })) {
      toOffscreenInOrder({ target: 'offscreen', type: 'rec.cancel' });
      abortVoice(live, 'The connection dropped; try again.');
      return;
    }
    live.sent = true;
    for (const event of live.pending.splice(0)) manager.send(event);
    if (live.recDone) sendAudioEnd(live);
  }

  function stopVoiceTurn(tabId: number, reason: PttReleaseReason): void {
    const turnId = recordingTurn;
    const live = turnId ? liveVoice.get(turnId) : undefined;
    if (!turnId || !live || live.record.tabId !== tabId) return;
    recordingTurn = null;
    live.record.releasedAt = Date.now();
    if (reason === 'cancel' || reason === 'tab-closed' || reason === 'navigated') {
      live.cancelled = true;
      live.record.outcome = 'cancelled';
      toOffscreenInOrder({ target: 'offscreen', type: 'rec.cancel' });
      if (live.sent) manager.send({ type: 'turn.cancel', turn_id: turnId });   // its turn.done reaches the tab
      else void toTab(tabId, { type: 'turn.event', event: { type: 'turn.done', turn_id: turnId, reason: 'cancelled' } });
      return;
    }
    toOffscreenInOrder({ target: 'offscreen', type: 'rec.stop', turnId });  // last chunk + rec.done follow
  }

  /** Speech of a voice turn -> the player; bookkeeping for the latency record. True when fully handled here. */
  function routeVoiceEvent(turnId: string, event: ServerEvent): boolean {
    const record = voiceRecord(turnId);
    if (!record) return false;
    switch (event.type) {
      case 'speech.segment':
        speakingTab = record.tabId;
        speakingTurn = turnId;
        toOffscreenInOrder({ target: 'offscreen', type: 'play.segment', turnId, seq: event.seq, mime: event.mime,
          sampleRate: event.sample_rate ?? null, text: event.text });
        return false;                                  // the tab gets the header too (captions)
      case 'speech.audio':
        toOffscreenInOrder({ target: 'offscreen', type: 'play.audio', turnId, seq: event.seq, data: event.data_b64 });
        return true;
      case 'speech.segment.end':
        toOffscreenInOrder({ target: 'offscreen', type: 'play.end', turnId, seq: event.seq });
        return false;
      case 'transcript.final':
        record.transcript = event.text;
        return false;
      case 'error':
        record.error = event.message;
        return false;
      case 'turn.done':
        if (record.outcome !== 'aborted') record.outcome = event.reason;   // ended here first (e.g. the mic died)
        if (event.metrics) record.metrics = event.metrics;
        liveVoice.delete(turnId);
        return false;
      default:
        return false;
    }
  }

  // ------------------------------------------------------------------------------------------- offscreen

  async function toOffscreen<T>(request: OffscreenRequest): Promise<T> {
    await ensureOffscreen();
    return (await browser.runtime.sendMessage(request)) as T;
  }

  async function probeMic(): Promise<MicProbeResult> {
    const result = await toOffscreen<MicProbeResult>({ target: 'offscreen', type: 'mic.probe' });
    const mic: MicInfo = result.ok
      ? { state: 'granted', updatedAt: Date.now() }
      : {
          state: result.error === 'NotAllowedError' ? 'needs-grant' : 'unavailable',
          error: result.error,
          updatedAt: Date.now(),
        };
    await browser.storage.session.set({ [MIC_KEY]: mic });
    return result;
  }

  // ------------------------------------------------------------------------------------------- requests

  async function handle(request: SwRequest, sender: Browser.runtime.MessageSender): Promise<unknown> {
    const tabId = sender.tab?.id;
    switch (request.type) {
      case 'ptt.down':
        if (tabId === undefined) return { ok: false, error: 'no tab' } satisfies Ack;
        ptt.down(tabId);
        return { ok: true } satisfies Ack;
      case 'ptt.up':
        if (tabId !== undefined) ptt.up(tabId, request.reason);
        return { ok: true } satisfies Ack;
      case 'status.get':
        return publicStatus(tabId) satisfies SwResponseMap['status.get'];
      case 'settings.get': {
        const settings = await loadSettings();
        return {
          pttChord: settings.pttChord,
          companionMode: settings.companionMode,
          siteDisabled: isSiteDisabled(sender.tab?.url, settings.disabledSites),
          devTimeline: settings.devTimeline,
        } satisfies SwResponseMap['settings.get'];
      }
      case 'settings.read':
        return toPagePrefs(await loadSettings()) satisfies SwResponseMap['settings.read'];
      case 'settings.set': {
        const check = validateSettingsPatch(request.patch);
        if (!check.ok) return check satisfies SwResponseMap['settings.set'];
        const saved = await saveSettings(check.values); // storage.onChanged reconnects if the credentials changed
        if (PREF_KEYS.some((key) => key in check.values)) void broadcast({ type: 'prefs.changed' }, true);
        return { ok: true, prefs: toPagePrefs(saved) } satisfies SwResponseMap['settings.set'];
      }
      case 'debug.state':
        return ((await toTab<DebugStateReply>(request.tabId, { type: 'debug.state' }))
          ?? { ok: false, error: 'no content script in that tab' }) satisfies SwResponseMap['debug.state'];
      case 'debug.scenePing':
        return ((await toTab<ScenePingReply>(request.tabId, { type: 'debug.scenePing' }))
          ?? { ok: false, error: 'no content script in that tab' }) satisfies SwResponseMap['debug.scenePing'];
      case 'debug.offscreenPing':
        return toOffscreen<OffscreenPingReply>({ target: 'offscreen', type: 'ping' });
      case 'debug.micProbe':
        return probeMic();
      case 'turn.text':
        if (tabId === undefined) return { ok: false, error: 'no tab' } satisfies SwResponseMap['turn.text'];
        return startTabTurn(tabId, request.text);
      case 'turn.stop':
        if (tabId !== undefined) cancelTabTurns(tabId);
        return { ok: true } satisfies Ack;
      case 'demo.run':
        return ((await toTab<Ack>(request.tabId, { type: 'demo.run' }))
          ?? { ok: false, error: 'no content script in that tab' }) satisfies SwResponseMap['demo.run'];
      case 'debug.pageContext': {
        const started = performance.now();
        const context = await collectPageContext(request.tabId,
          request.budget ? NAMED_PAGE_BUDGETS[request.budget] : undefined);
        if (!context) return { ok: false, error: 'no content script in that tab' } satisfies SwResponseMap['debug.pageContext'];
        const totalMs = Math.round(performance.now() - started);
        return { ok: true, ...context, totalMs } satisfies SwResponseMap['debug.pageContext'];
      }
      case 'debug.marks': {
        const budget = snapshotBudget(manager.conn.pageBudget ?? DEFAULT_PAGE_BUDGET);
        return ((await toTab<Ack>(request.tabId, { type: 'debug.marks', budget }))
          ?? { ok: false, error: 'no content script in that tab' }) satisfies SwResponseMap['debug.marks'];
      }
      case 'debug.voice':
        return { turns: voiceRecords.map((record) => ({ ...record })) } satisfies SwResponseMap['debug.voice'];
      case 'conversation.reset':
        return (manager.send({ type: 'conversation.reset' })
          ? { ok: true } : { ok: false, error: 'not connected to the backend' }) satisfies SwResponseMap['conversation.reset'];
      case 'scene.result': {
        // Only the tab whose stage got the scene may answer for it.
        if (tabId === undefined || sceneTabs.get(request.scene_id) !== tabId) {
          return { ok: false, error: 'the scene belongs to another tab' } satisfies Ack;
        }
        const { type: _type, ...result } = request;
        return (manager.send({ type: 'scene.result', ...result })
          ? { ok: true } : { ok: false, error: 'not connected to the backend' }) satisfies SwResponseMap['scene.result'];
      }
      case 'consent.get':
        return tasks.view(request.taskId, sender) satisfies SwResponseMap['consent.get'];
      case 'consent.decide':
        return ((await tasks.decide(request.taskId, request.decision, sender))
          ? { ok: true } : { ok: false, error: 'This request is no longer open.' }) satisfies SwResponseMap['consent.decide'];
      case 'task.stop':
        return (tasks.stop(request.taskId, tabId)
          ? { ok: true } : { ok: false, error: 'not this tab\u2019s task' }) satisfies SwResponseMap['task.stop'];
      case 'task.answer':
        return (tasks.answer(request.taskId, request.requestId, request.approved, tabId)
          ? { ok: true } : { ok: false, error: 'not this tab\u2019s task' }) satisfies SwResponseMap['task.answer'];
      case 'task.open':
        return ((await tasks.open(request.taskId, request.url, tabId))
          ? { ok: true } : { ok: false, error: 'not a page this task may open' }) satisfies SwResponseMap['task.open'];
      case 'grants.list':
        return { grants: await tasks.grants() } satisfies SwResponseMap['grants.list'];
      case 'grants.clear':
        await tasks.clearGrants();
        return { ok: true } satisfies SwResponseMap['grants.clear'];
      case 'config.get':
        return (await askConfig(null)) satisfies SwResponseMap['config.get'];
      case 'config.set':
        return (await askConfig(request.change)) satisfies SwResponseMap['config.set'];
      case 'grants.remove':
        await tasks.removeGrant(request.site);
        return { ok: true } satisfies SwResponseMap['grants.remove'];
      case 'debug.taskAnswer':
        return ((await toTab<Ack>(request.tabId, { type: 'debug.taskAnswer', approved: request.approved }))
          ?? { ok: false, error: 'no content script in that tab' }) satisfies SwResponseMap['debug.taskAnswer'];
      case 'debug.sceneClose':
        return ((await toTab<Ack>(request.tabId, { type: 'debug.sceneClose' }))
          ?? { ok: false, error: 'no content script in that tab' }) satisfies SwResponseMap['debug.sceneClose'];
      case 'debug.sceneRender':
        return ((await toTab<SwResponseMap['debug.sceneRender']>(request.tabId, {
          type: 'debug.sceneRender', kind: request.kind, code: request.code, title: request.title ?? 'Debug scene',
        }, false, SCENE_DEBUG_TIMEOUT_MS)) ?? { ok: false, error: 'no content script in that tab' }) satisfies SwResponseMap['debug.sceneRender'];
      // Offscreen recorder/player traffic: synchronous up to the socket send, so chunks keep their order.
      case 'rec.chunk': {
        const live = liveVoice.get(request.turnId);
        // A turn this worker doesn't know (it restarted, or gave the turn up): the document stops recording.
        if (!live || live.cancelled) return { ok: false, error: 'stale turn' } satisfies Ack;
        live.record.chunks += 1;
        const event: ClientEvent = { type: 'audio.chunk', turn_id: request.turnId, seq: request.seq,
          data_b64: request.data, mime: request.mime };
        if (live.sent) manager.send(event);
        else live.pending.push(event);
        return { ok: true } satisfies Ack;
      }
      case 'rec.done': {
        const live = liveVoice.get(request.turnId);
        if (live && !live.cancelled) {
          live.recDone = true;
          if (live.sent) sendAudioEnd(live);
        }
        return { ok: true } satisfies Ack;
      }
      case 'rec.level': {
        const live = liveVoice.get(request.turnId);
        if (live) void toTab(live.record.tabId, { type: 'voice.level', source: 'mic', level: request.level });
        return { ok: true } satisfies Ack;
      }
      case 'play.level':
        if (speakingTab !== null) void toTab(speakingTab, { type: 'voice.level', source: 'speech', level: request.level });
        return { ok: true } satisfies Ack;
      case 'play.event': {
        const record = voiceRecord(request.turnId);
        if (record && request.state === 'started' && request.seq === 0 && record.firstAudioAt === undefined) {
          record.firstAudioAt = Date.now() + request.inMs;
          if (record.releasedAt !== undefined) record.latencyMs = record.firstAudioAt - record.releasedAt;
        }
        if (request.state !== 'idle') {
          manager.send({ type: 'playback.event', turn_id: request.turnId, seq: request.seq, state: request.state,
            at_ms: Date.now() + request.inMs });
        }
        const tabId = record?.tabId;
        if (tabId !== undefined) {
          void toTab(tabId, { type: 'speech.playback', turnId: request.turnId, seq: request.seq, state: request.state,
            inMs: request.inMs });
        }
        return { ok: true } satisfies Ack;
      }
    }
  }

  browser.runtime.onMessage.addListener((raw: unknown, sender, sendResponse) => {
    const request = parseSwRequest(raw);
    if (!request) return undefined; // offscreen traffic or noise: not for the worker
    const kind = classifySender(sender as SenderLike, extensionId);
    if (!isAllowed(request.type, kind)) {
      log.warn('refused message', { type: request.type, kind });
      sendResponse({ ok: false, error: 'not allowed from this context' });
      return undefined;
    }
    void manager.ensureConnected('message');
    handle(request, sender).then(sendResponse, (error: unknown) => {
      log.error(`${request.type} failed`, error);
      sendResponse({ ok: false, error: error instanceof Error ? error.message : String(error) });
    });
    return true; // async sendResponse
  });

  // ------------------------------------------------------------------------------------------- offscreen lifeline

  /**
   * The offscreen document died (Chrome closed it, or it crashed) while it mattered: the voice turn it was recording
   * ends with a message instead of waiting for audio that will never come, and a tab it was speaking to is told the
   * speech stopped. The next push-to-talk recreates the document (ensureOffscreen).
   */
  function offscreenLost(): void {
    const live = recordingTurn ? liveVoice.get(recordingTurn) : undefined;
    if (live) {
      if (live.sent) manager.send({ type: 'turn.cancel', turn_id: live.record.turnId });
      abortVoice(live, 'The microphone stopped unexpectedly. Please try again.');
      ptt.releaseTab(live.record.tabId, 'cancel');
    }
    if (speakingTab !== null && speakingTurn !== null) {
      void toTab(speakingTab, { type: 'speech.playback', turnId: speakingTurn, seq: 0, state: 'idle', inMs: 0 });
    }
    speakingTab = null;
    speakingTurn = null;
    offscreenQueue = Promise.resolve();         // nothing queued for the old document may hold up the new one
  }

  browser.runtime.onConnect.addListener((port) => {
    if (port.name !== OFFSCREEN_LIFELINE_PORT) return;
    if (classifySender((port.sender ?? {}) as SenderLike, extensionId) !== 'extension-page') {
      port.disconnect();
      return;
    }
    port.onDisconnect.addListener(() => {
      if (recordingTurn !== null || speakingTab !== null) log.warn('the offscreen document went away mid-turn');
      offscreenLost();
    });
  });

  // ------------------------------------------------------------------------------------------- debug turns

  browser.runtime.onConnect.addListener((port) => {
    if (port.name !== DEBUG_TURN_PORT) return;
    if (classifySender((port.sender ?? {}) as SenderLike, extensionId) !== 'extension-page') {
      port.disconnect();
      return;
    }
    let current: string | null = null;
    port.onMessage.addListener((raw: unknown) => {
      const command = parseDebugTurnCommand(raw);
      if (!command) return;
      if (command.type === 'turn.cancel') {
        if (current) manager.send({ type: 'turn.cancel', turn_id: current });
        return;
      }
      if (!manager.isOpen) {
        safePost(port, { type: 'turn.rejected', error: `Not connected (${manager.conn.state}).` });
        void manager.ensureConnected('debug-turn');
        return;
      }
      current = crypto.randomUUID();
      debugTurns.set(current, port);
      if (!manager.send({ type: 'turn.start', turn_id: current, mode: 'text', text: command.text })) {
        debugTurns.delete(current);
        safePost(port, { type: 'turn.rejected', error: 'The connection dropped; try again.' });
        return;
      }
      safePost(port, { type: 'turn.accepted', turn_id: current });
    });
    port.onDisconnect.addListener(() => {
      for (const [turnId, owner] of debugTurns) {
        if (owner !== port) continue;
        debugTurns.delete(turnId);
        manager.send({ type: 'turn.cancel', turn_id: turnId });
      }
    });
  });

  // ------------------------------------------------------------------------------------------- browser events

  browser.tabs.onActivated.addListener(({ tabId }) => ptt.focusChanged(tabId, 'tab-switch'));
  browser.windows.onRemoved.addListener((windowId) => tasks.onWindowRemoved(windowId));   // closed consent = no
  browser.windows.onFocusChanged.addListener((windowId) => {
    if (windowId === browser.windows.WINDOW_ID_NONE) ptt.focusChanged(null, 'window-blur');
  });
  browser.tabs.onRemoved.addListener((tabId) => {
    ptt.releaseTab(tabId, 'tab-closed');
    cancelTabTurns(tabId);
    if (speakingTab === tabId) {
      toOffscreenInOrder({ target: 'offscreen', type: 'play.stop' });
      speakingTab = null;
    }
    orbStates.delete(tabId);
  });
  browser.tabs.onUpdated.addListener((tabId, change) => {
    if (change.status !== 'loading') return;
    ptt.releaseTab(tabId, 'navigated');
    cancelTabTurns(tabId); // the companion that asked is gone
  });

  browser.commands.onCommand.addListener((command, tab) => {
    void manager.ensureConnected('command');
    if (tab?.id === undefined) return;
    if (command === 'toggle-listen') ptt.toggle(tab.id);
    else if (command === 'toggle-companion') void toTab(tab.id, { type: 'orb.toggle' });
  });

  browser.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === RECONNECT_ALARM) void manager.ensureConnected('alarm');
  });

  browser.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    if (CONNECTION_KEYS.some((key) => key in changes)) void manager.reset('settings');
    // "Always" grants were given to the agent behind the old backend: pairing with another one starts without.
    const url = changes.backendUrl;
    if (url && url.oldValue !== undefined && url.oldValue !== url.newValue) void tasks.clearGrants();
  });

  browser.runtime.onStartup.addListener(() => void manager.ensureConnected('startup'));

  browser.runtime.onInstalled.addListener((details) => {
    void manager.ensureConnected('installed');
    void injectIntoOpenTabs();
    if (details.reason === 'install') {
      void loadSettings().then((settings) => {
        if (!settings.token) void browser.tabs.create({ url: browser.runtime.getURL('/onboarding.html') });
      });
    }
  });

  /** Manifest content scripts only reach pages loaded after install; inject into the ones already open. */
  async function injectIntoOpenTabs(): Promise<void> {
    const tabs = await browser.tabs.query({ url: ['http://*/*', 'https://*/*'] });
    await Promise.all(tabs.map(async (tab) => {
      if (tab.id === undefined) return;
      await browser.scripting.executeScript({ target: { tabId: tab.id }, files: ['/content-scripts/content.js'] })
        .catch(() => undefined);
      await browser.scripting
        .executeScript({ target: { tabId: tab.id, allFrames: true }, files: ['/content-scripts/hotkeys.js'] })
        .catch(() => undefined);
    }));
  }

  void manager.ensureConnected('worker-start');
  // Whatever an earlier worker was doing for a tab (a turn, a task, push-to-talk) ended with it.
  void broadcast({ type: 'worker.started' });
});

const TAB_TURN_EVENTS: ReadonlySet<string> = new Set<string>(TAB_TURN_EVENT_TYPES);

function isTabTurnEvent(event: ServerEvent): event is TabTurnEvent {
  return TAB_TURN_EVENTS.has(event.type);
}

function safePost(port: Browser.runtime.Port, message: DebugTurnEvent): void {
  try {
    port.postMessage(message);
  } catch {
    // popup closed meanwhile
  }
}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timed out after ${ms} ms`)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error: unknown) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}
