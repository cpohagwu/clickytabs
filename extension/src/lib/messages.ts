/**
 * Extension-internal message contract (service worker ⇄ content scripts ⇄ extension pages ⇄ offscreen document)
 * and the sender rules the service worker enforces.
 *
 * Trust model:
 * - Only messages from this extension (`sender.id`) are considered at all.
 * - Extension pages (`chrome-extension://<id>/…`) may send everything.
 * - Content scripts in any frame may only relay raw PTT key events and read non-sensitive state.
 * - Anything sensitive (settings writes, debug hooks, page data and consent) needs an extension page or the top
 *   frame (`frameId` 0) of the turn's tab.
 *
 * This module is pure (no extension APIs) so it can be shared by every context and unit-tested.
 */
import type { ConnState } from './conn';
import { isConnState } from './conn';
import type { CompanionMode, ContentPrefs, PttChord } from './prefs';
import { isCompanionMode, isPttChord } from './prefs';
import type { PageBudgetName } from './page-context/capture';
import type { SnapshotBudget } from './page-context/snapshot';
import type { SceneEvent, StageDebug } from './scene/stage';
import { isSceneKind, MAX_SCENE_CODE_CHARS, type RenderResult, type SceneKind } from './scene/types';
import type { Grants } from './task/sites';
import type { TurnTimeline } from './timeline';
import type { TaskEvent, TrackerState } from './task/tracker';
import type {
  AssistantTextDelta,
  ConfigState,
  ErrorEvent,
  PageContext,
  ProviderChoiceWire,
  SpeechSegment,
  SpeechSegmentEnd,
  TranscriptFinal,
  TranscriptPartial,
  TurnDone,
  TurnState,
  VisualDirectiveEvent,
} from './protocol.gen';

// ------------------------------------------------------------------------------------------------- value types

export const ORB_STATES = ['idle', 'listening', 'thinking', 'speaking', 'acting', 'error'] as const;
export type OrbState = (typeof ORB_STATES)[number];

/** Why a frame reports that the push-to-talk chord ended. */
export const FRAME_RELEASE_REASONS = ['keyup', 'blur', 'hidden', 'pagehide', 'cancel'] as const;
export type FrameReleaseReason = (typeof FRAME_RELEASE_REASONS)[number];

/** Connection status as content scripts see it. */
export interface PublicStatus {
  state: ConnState;
  orb: OrbState;
}

/** Settings as extension pages see them: the token itself is never echoed back. */
export interface PagePrefs {
  backendUrl: string;
  hasToken: boolean;
  pttChord: PttChord;
  companionMode: CompanionMode;
  disabledSites: string[];
  devTimeline: boolean;
}

export interface SettingsPatch {
  backendUrl?: string;
  token?: string;
  pttChord?: PttChord;
  companionMode?: CompanionMode;
  disabledSites?: string[];
  devTimeline?: boolean;
}

export type Ack = { ok: true } | { ok: false; error: string };

// ------------------------------------------------------------------------------------ requests to the worker

export type SwRequest =
  | { type: 'ptt.down' }
  | { type: 'ptt.up'; reason: FrameReleaseReason }
  | { type: 'status.get' }
  | { type: 'settings.get' }
  | { type: 'settings.read' }
  | { type: 'settings.set'; patch: SettingsPatch }
  | { type: 'debug.state'; tabId: number }
  | { type: 'debug.scenePing'; tabId: number }
  | { type: 'debug.offscreenPing' }
  | { type: 'debug.micProbe' }
  | { type: 'turn.text'; text: string }
  | { type: 'turn.stop' }
  | { type: 'demo.run'; tabId: number }
  | { type: 'debug.pageContext'; tabId: number; budget?: PageBudgetName }
  | { type: 'debug.marks'; tabId: number }
  | { type: 'debug.voice' }
  /** Popup: forget the conversation so far. */
  | { type: 'conversation.reset' }
  /** The companion's stage ran (or turned down) a scene.render; only from the tab that owns the scene. */
  | { type: 'scene.result'; scene_id: string; attempt: number; ok: boolean; error?: string; unavailable?: boolean }
  /** Tests: render code straight into a tab's stage, without the backend. */
  | { type: 'debug.sceneRender'; tabId: number; kind: SceneKind; code: string; title?: string }
  /** Tests: close a tab's stage (its shadow root is closed, so its button can't be clicked from outside). */
  | { type: 'debug.sceneClose'; tabId: number }
  /** The consent window: what it is asking about, and the user's answer. */
  | { type: 'consent.get'; taskId: string }
  | { type: 'consent.decide'; taskId: string; decision: ConsentDecision }
  /** The tracker in the tab that owns the task. */
  | { type: 'task.stop'; taskId: string }
  | { type: 'task.answer'; taskId: string; requestId: string; approved: boolean }
  | { type: 'task.open'; taskId: string; url: string }
  /** Options: the sites a task may use without asking ("Always"). */
  | { type: 'grants.list' }
  | { type: 'grants.remove'; site: string }
  | { type: 'grants.clear' }
  /** Options: the backend's profile, providers and voices; switching them without a restart. */
  | { type: 'config.get' }
  | { type: 'config.set'; change: ConfigChange }
  /** Tests: answer the tracker's question in a tab (its buttons are in a closed shadow root). */
  | { type: 'debug.taskAnswer'; tabId: number; approved: boolean }
  // offscreen document -> worker
  | { type: 'rec.chunk'; turnId: string; seq: number; data: string; mime: string }
  | { type: 'rec.level'; turnId: string; level: number }
  | { type: 'rec.done'; turnId: string; chunks: number; durationMs: number }
  | { type: 'play.event'; turnId: string; seq: number; state: PlaybackState; inMs: number; durationMs?: number }
  | { type: 'play.level'; turnId: string; level: number };
export type SwRequestType = SwRequest['type'];

export const PLAYBACK_STATES = ['started', 'ended', 'interrupted', 'idle'] as const;
export type PlaybackState = (typeof PLAYBACK_STATES)[number];

/** One push-to-talk turn as the worker saw it (for debugging and the latency measurements). */
export interface VoiceTurnRecord {
  turnId: string;
  tabId: number;
  /** Date.now() of key down, key release and the first audible answer. */
  pressedAt: number;
  releasedAt?: number;
  firstAudioAt?: number;
  /** Key release -> first audible answer, as the user experiences it. */
  latencyMs?: number;
  micStartMs?: number;
  chunks: number;
  transcript?: string;
  outcome?: string;
  error?: string;
  /** The backend's own breakdown (turn.done metrics): stt_ms, brain_first_token_ms, first_audio_ms, total_ms. */
  metrics?: Partial<Record<string, number>>;
}

/** Geometry and hit-testing of the companion, measured inside its closed shadow root (e2e + debugging). */
export interface LayoutProbe {
  popoverOpen: boolean;
  viewport: { width: number; height: number };
  orb: { x: number; y: number; width: number; height: number };
  canvas: { width: number; height: number; cssWidth: number; cssHeight: number };
  /** What a click at the orb's center would reach: the orb, another part of the companion, or the page. */
  hitAtOrb: 'orb' | 'companion' | 'page';
}

export interface DebugStateReply {
  mounted: boolean;
  visible: boolean;
  orbState: OrbState;
  conn: ConnState | 'unknown';
  /** True when `chrome.storage.local` is closed to content scripts (setAccessLevel TRUSTED_CONTEXTS took effect). */
  storageLocked: boolean;
  companionMode: CompanionMode;
  hudOpen: boolean;
  /** Annotations currently drawn on the overlay, and whether any flight/animation is still running. */
  annotations: number;
  animating: boolean;
  /** The latest brain directive applied, and the text of the element it targeted. */
  lastDirective: { kind: string; target: string | null } | null;
  /** The last turn's latency timeline, and whether the HUD shows it (developer option). */
  timeline: TurnTimeline | null;
  devTimeline: boolean;
  /** The error the HUD shows, if any, and whether a turn is in flight. */
  error: string | null;
  busy: boolean;
  /** Latest assistant caption (what the HUD shows). */
  caption: string;
  layout: LayoutProbe | null;
  /** The scene stage. */
  scene: StageDebug | null;
  /** The browser-task tracker. */
  task: (Omit<TrackerState, 'thumb'> & { hasThumb: boolean }) | null;
}

export const CONSENT_DECISIONS = ['allow_once', 'always', 'deny'] as const;
export type ConsentDecision = (typeof CONSENT_DECISIONS)[number];

/** What the consent window shows (all of it worked out by the worker from the real tab, never from the model). */
export interface ConsentView {
  taskId: string;
  goal: string;
  tabSite: string;
  taskSite: string;
  crossSite: boolean;
  session: boolean;
  cookieCount: number;
  startUrl: string | null;
  tabTitle: string;
}

export type ScenePingReply =
  | { ok: true; pong: { type: 'pong'; at: number }; rttMs: number }
  | { ok: false; error: string };

export type OffscreenPingReply = { ok: true; type: 'pong'; at: number; recording?: boolean } | { ok: false; error: string };

export type MicProbeResult = { ok: true } | { ok: false; error: string };

export type MicState = 'unknown' | 'granted' | 'needs-grant' | 'unavailable';
export const MIC_KEY = 'mic';
export interface MicInfo {
  state: MicState;
  error?: string;
  updatedAt: number;
}

export type SettingsSetResponse = { ok: true; prefs: PagePrefs } | { ok: false; error: string };

export type TurnTextResponse = { ok: true; turn_id: string } | { ok: false; error: string };

/** What a turn would send as page context right now, with timings (the screenshot is base64 inside `page`). */
export type PageContextReply =
  | { ok: true; page: PageContext; snapshotMs: number; totalMs: number; capture: CaptureReport; phases: Record<string, number> }
  | { ok: false; error: string };

/** How the screenshot went: attempts made, how many were stale (no sentinel), and the error if one was thrown. */
export interface CaptureReport {
  attempts: number;
  stale: number;
  error?: string;
}

export interface ImageSize {
  width: number;
  height: number;
}

/** Response type for each request type. */
export interface SwResponseMap {
  'ptt.down': Ack;
  'ptt.up': Ack;
  'status.get': PublicStatus;
  'settings.get': ContentPrefs;
  'settings.read': PagePrefs;
  'settings.set': SettingsSetResponse;
  'debug.state': DebugStateReply | { ok: false; error: string };
  'debug.scenePing': ScenePingReply;
  'debug.offscreenPing': OffscreenPingReply;
  'debug.micProbe': MicProbeResult;
  'turn.text': TurnTextResponse;
  'turn.stop': Ack;
  'demo.run': Ack;
  'debug.pageContext': PageContextReply;
  'debug.marks': Ack;
  'debug.voice': { turns: VoiceTurnRecord[] };
  'conversation.reset': Ack;
  'scene.result': Ack;
  'debug.sceneRender': RenderResult | { ok: false; error: string };
  'debug.sceneClose': Ack;
  'consent.get': ConsentView | { ok: false; error: string };
  'consent.decide': Ack;
  'task.stop': Ack;
  'task.answer': Ack;
  'task.open': Ack;
  'grants.list': { grants: Grants };
  'grants.remove': Ack;
  'grants.clear': Ack;
  'config.get': ConfigState | { ok: false; error: string };
  'config.set': ConfigState | { ok: false; error: string };
  'debug.taskAnswer': Ack;
  'rec.chunk': Ack;
  'rec.level': Ack;
  'rec.done': Ack;
  'play.event': Ack;
  'play.level': Ack;
}

// ----------------------------------------------------------------------- worker → content scripts (per tab)

export type TabMessage =
  | { type: 'status.changed'; status: PublicStatus }
  /** A new service worker started: whatever the old one was doing for this tab (turn, task, recording) is gone. */
  | { type: 'worker.started' }
  | { type: 'orb.state'; state: OrbState }
  | { type: 'orb.toggle' }
  | { type: 'ptt.state'; holding: boolean }
  | { type: 'prefs.changed' }
  | { type: 'debug.state' }
  | { type: 'debug.scenePing' }
  | { type: 'turn.event'; event: TabTurnEvent }
  | { type: 'demo.run' }
  /** Snapshot the DOM for a turn; with `hideUi`, also hide the companion until `context.restore` (screenshot). */
  | { type: 'context.snapshot'; budget: SnapshotBudget; hideUi: boolean }
  /** Capture finished: show the companion again; `image` is the screenshot size the brain will see (or null). */
  | { type: 'context.restore'; image: ImageSize | null }
  | { type: 'debug.marks'; budget: SnapshotBudget }
  /** A push-to-talk turn started in this tab. */
  | { type: 'voice.turn'; turnId: string }
  /** A voice turn could not start (no backend, no microphone). */
  | { type: 'voice.error'; message: string }
  /** Mic level while recording, speech level while answering (0..1). */
  | { type: 'voice.level'; source: 'mic' | 'speech'; level: number }
  /** A spoken segment became audible in `inMs`, ended, was interrupted, or all audio so far finished (idle). */
  | { type: 'speech.playback'; turnId: string; seq: number; state: PlaybackState; inMs: number }
  /** A scene of a turn from this tab: building, code to render, or failed. */
  | { type: 'scene.event'; event: SceneEvent }
  | { type: 'debug.sceneRender'; kind: SceneKind; code: string; title: string }
  | { type: 'debug.sceneClose' }
  /** A browser task this tab asked for: consent, start, steps, questions, result. */
  | { type: 'task.event'; event: TaskEvent }
  /** The tab origin's localStorage, for a task the user allowed to use their login. */
  | { type: 'context.storage' }
  | { type: 'debug.taskAnswer'; approved: boolean };

/** Events of a turn started from this tab (HUD or push-to-talk), forwarded by the worker. */
export type TabTurnEvent =
  | TurnState
  | AssistantTextDelta
  | TurnDone
  | ErrorEvent
  | VisualDirectiveEvent
  | TranscriptPartial
  | TranscriptFinal
  | SpeechSegment
  | SpeechSegmentEnd;

export const TAB_TURN_EVENT_TYPES = ['turn.state', 'assistant.text.delta', 'turn.done', 'error', 'visual.directive',
  'transcript.partial', 'transcript.final', 'speech.segment', 'speech.segment.end'] as const;

// ------------------------------------------------------------------------------- worker → offscreen document

export type OffscreenRequest =
  | { target: 'offscreen'; type: 'ping' }
  | { target: 'offscreen'; type: 'mic.probe' }
  | { target: 'offscreen'; type: 'rec.start'; turnId: string }
  | { target: 'offscreen'; type: 'rec.stop'; turnId: string }
  | { target: 'offscreen'; type: 'rec.cancel' }
  | { target: 'offscreen'; type: 'play.segment'; turnId: string; seq: number; mime: string; sampleRate: number | null;
      text: string }
  | { target: 'offscreen'; type: 'play.audio'; turnId: string; seq: number; data: string }
  | { target: 'offscreen'; type: 'play.end'; turnId: string; seq: number }
  | { target: 'offscreen'; type: 'play.stop' };

export type RecStartReply = { ok: true; startMs: number } | { ok: false; error: string; name: string };

// ------------------------------------------------------------------------- popup ⇄ worker: debug text turns

export const DEBUG_TURN_PORT = 'debug-turn';
/**
 * The offscreen document keeps this port open to the worker. When the worker dies, the document's end
 * disconnects: it stops recording (the mic goes off) and playing, then reconnects, which starts a new worker at once.
 * When the document dies, the worker's end disconnects: it ends the voice turn that was recording or speaking.
 */
export const OFFSCREEN_LIFELINE_PORT = 'offscreen-lifeline';

// ------------------------------------------------------------------------------------------- runtime config

export const CONFIG_CAPABILITIES = ['brain', 'codegen', 'agent_llm', 'stt', 'tts', 'summarizer', 'embeddings'] as const;
export const CONFIG_PROFILES = ['local', 'cloud', 'hybrid'] as const;
/** Mirrors the backend's UI_OPTION_KEYS: base URLs and hosts can only be set in clicky.toml. */
export const CONFIG_OPTION_KEYS = ['voice', 'voice_id', 'effort', 'speed'] as const;

export interface ConfigChange {
  profile?: (typeof CONFIG_PROFILES)[number];
  capability?: (typeof CONFIG_CAPABILITIES)[number];
  choice?: ProviderChoiceWire | null;
  reset?: boolean;
}

function parseChoice(raw: unknown): ProviderChoiceWire | null {
  if (!isRecord(raw) || typeof raw.provider !== 'string' || !/^[a-z][a-z0-9_]{0,31}$/.test(raw.provider)) return null;
  const choice: ProviderChoiceWire = { provider: raw.provider };
  if (raw.model !== undefined && raw.model !== null) {
    if (typeof raw.model !== 'string' || raw.model.length < 1 || raw.model.length > 200) return null;
    choice.model = raw.model;
  }
  if (raw.options !== undefined) {
    if (!isRecord(raw.options)) return null;
    const options: Record<string, string | number | boolean> = {};
    for (const [key, value] of Object.entries(raw.options)) {
      if (!(CONFIG_OPTION_KEYS as readonly string[]).includes(key)) return null;
      if (typeof value === 'string' && value.length <= 200) options[key] = value;
      else if ((typeof value === 'number' && Number.isFinite(value)) || typeof value === 'boolean') options[key] = value;
      else return null;
    }
    choice.options = options;
  }
  return choice;
}

export function parseConfigChange(raw: unknown): ConfigChange | null {
  if (!isRecord(raw)) return null;
  const change: ConfigChange = {};
  if (raw.profile !== undefined) {
    if (!(CONFIG_PROFILES as readonly unknown[]).includes(raw.profile)) return null;
    change.profile = raw.profile as ConfigChange['profile'];
  }
  if (raw.capability !== undefined) {
    if (!(CONFIG_CAPABILITIES as readonly unknown[]).includes(raw.capability)) return null;
    change.capability = raw.capability as ConfigChange['capability'];
  }
  if (raw.reset !== undefined) {
    if (typeof raw.reset !== 'boolean') return null;
    change.reset = raw.reset;
  }
  if (raw.choice !== undefined && raw.choice !== null) {
    const choice = parseChoice(raw.choice);
    if (!choice) return null;
    change.choice = choice;
  }
  if (change.capability === undefined && change.profile === undefined) return null;
  if (change.capability !== undefined && !change.choice && !change.reset) return null;
  return change;
}

export type DebugTurnCommand = { type: 'turn.start'; text: string } | { type: 'turn.cancel' };

/** Server events for the port's turn are forwarded verbatim, plus two local acknowledgements. */
export type DebugTurnEvent =
  | { type: 'turn.accepted'; turn_id: string }
  | { type: 'turn.rejected'; error: string }
  | TurnState
  | AssistantTextDelta
  | TurnDone
  | ErrorEvent;

export const MAX_DEBUG_TURN_CHARS = 4000;
export const MAX_TURN_TEXT_CHARS = 4000;

// ------------------------------------------------------------------------------------------------ sender rules

export type SenderKind = 'extension-page' | 'content-top' | 'content-frame' | 'foreign';

/** The subset of `chrome.runtime.MessageSender` the rules look at. */
export interface SenderLike {
  id?: string;
  url?: string;
  origin?: string;
  frameId?: number;
  tab?: { id?: number };
}

export function classifySender(sender: SenderLike, extensionId: string): SenderKind {
  if (!extensionId || sender.id !== extensionId) return 'foreign';
  const origin = `chrome-extension://${extensionId}`;
  if (sender.url?.startsWith(`${origin}/`)) {
    // `origin` is absent on some older senders; when present it must agree with the URL.
    return sender.origin === undefined || sender.origin === origin ? 'extension-page' : 'foreign';
  }
  if (sender.tab?.id === undefined || sender.frameId === undefined) return 'foreign';
  return sender.frameId === 0 ? 'content-top' : 'content-frame';
}

type Audience = 'any-frame' | 'top-frame' | 'extension-page';

/** Who may send each request. `Record` keeps this exhaustive when request types are added. */
export const REQUEST_POLICY: Record<SwRequestType, Audience> = {
  'ptt.down': 'any-frame',
  'ptt.up': 'any-frame',
  'status.get': 'any-frame',
  'settings.get': 'any-frame', // content scripts get ContentPrefs only (chord + "is this site disabled")
  'settings.read': 'extension-page',
  'settings.set': 'extension-page',
  'debug.state': 'extension-page',
  'debug.scenePing': 'extension-page',
  'debug.offscreenPing': 'extension-page',
  'debug.micProbe': 'extension-page',
  'turn.text': 'top-frame', // the companion HUD; the turn carries page data, which only the top frame may send
  'turn.stop': 'top-frame',
  'demo.run': 'extension-page',
  'debug.pageContext': 'extension-page',
  'debug.marks': 'extension-page',
  'debug.voice': 'extension-page',
  'conversation.reset': 'extension-page',
  'scene.result': 'top-frame', // and the tab must own the scene (checked by the worker)
  'debug.sceneRender': 'extension-page',
  'debug.sceneClose': 'extension-page',
  'consent.get': 'extension-page', // and only the consent window the worker opened for that task
  'consent.decide': 'extension-page',
  'task.stop': 'top-frame', // and only the tab that owns the task
  'task.answer': 'top-frame',
  'task.open': 'top-frame',
  'grants.list': 'extension-page',
  'grants.remove': 'extension-page',
  'grants.clear': 'extension-page',
  'config.get': 'extension-page',
  'config.set': 'extension-page',
  'debug.taskAnswer': 'extension-page',
  'rec.chunk': 'extension-page', // the offscreen document
  'rec.level': 'extension-page',
  'rec.done': 'extension-page',
  'play.event': 'extension-page',
  'play.level': 'extension-page',
};

export function isAllowed(type: SwRequestType, kind: SenderKind): boolean {
  const audience = REQUEST_POLICY[type];
  switch (kind) {
    case 'extension-page':
      return true;
    case 'content-top':
      return audience !== 'extension-page';
    case 'content-frame':
      return audience === 'any-frame';
    default:
      return false;
  }
}

// ----------------------------------------------------------------------------------------------- parsing

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isTabId(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0;
}

export function isFrameReleaseReason(value: unknown): value is FrameReleaseReason {
  return typeof value === 'string' && (FRAME_RELEASE_REASONS as readonly string[]).includes(value);
}

export function isOrbState(value: unknown): value is OrbState {
  return typeof value === 'string' && (ORB_STATES as readonly string[]).includes(value);
}

/** Structural check of a settings patch; semantic checks (URL policy, site syntax) happen in settings.ts. */
export function parseSettingsPatch(value: unknown): SettingsPatch | null {
  if (!isRecord(value)) return null;
  const patch: SettingsPatch = {};
  for (const [key, item] of Object.entries(value)) {
    switch (key) {
      case 'backendUrl':
      case 'token':
        if (typeof item !== 'string') return null;
        patch[key] = item;
        break;
      case 'pttChord':
        if (!isPttChord(item)) return null;
        patch.pttChord = item;
        break;
      case 'companionMode':
        if (!isCompanionMode(item)) return null;
        patch.companionMode = item;
        break;
      case 'disabledSites':
        if (!Array.isArray(item) || !item.every((site) => typeof site === 'string')) return null;
        patch.disabledSites = [...item];
        break;
      case 'devTimeline':
        if (typeof item !== 'boolean') return null;
        patch.devTimeline = item;
        break;
      default:
        return null;
    }
  }
  return patch;
}

/**
 * Validates an incoming runtime message and returns a fresh object containing only known fields, or null.
 * Messages addressed to the offscreen document (`target: 'offscreen'`) are not worker requests.
 */
export function parseSwRequest(raw: unknown): SwRequest | null {
  if (!isRecord(raw) || typeof raw.type !== 'string' || 'target' in raw) return null;
  switch (raw.type) {
    case 'ptt.down':
    case 'status.get':
    case 'settings.get':
    case 'settings.read':
    case 'debug.offscreenPing':
    case 'debug.micProbe':
    case 'turn.stop':
    case 'debug.voice':
    case 'conversation.reset':
      return { type: raw.type };
    case 'rec.chunk':
      return isId(raw.turnId) && isCount(raw.seq, 100_000) && typeof raw.data === 'string'
        && raw.data.length <= 1_000_000 && typeof raw.mime === 'string'
        ? { type: 'rec.chunk', turnId: raw.turnId, seq: raw.seq, data: raw.data, mime: raw.mime.slice(0, 100) }
        : null;
    case 'rec.level':
    case 'play.level':
      return isId(raw.turnId) && isLevel(raw.level) ? { type: raw.type, turnId: raw.turnId, level: raw.level } : null;
    case 'rec.done':
      return isId(raw.turnId) && isCount(raw.chunks, 100_000) && typeof raw.durationMs === 'number'
        ? { type: 'rec.done', turnId: raw.turnId, chunks: raw.chunks, durationMs: raw.durationMs }
        : null;
    case 'play.event': {
      if (!isId(raw.turnId) || !isCount(raw.seq, 100_000) || !isPlaybackState(raw.state)) return null;
      if (typeof raw.inMs !== 'number') return null;
      const event: SwRequest = { type: 'play.event', turnId: raw.turnId, seq: raw.seq, state: raw.state, inMs: raw.inMs };
      if (typeof raw.durationMs === 'number') event.durationMs = raw.durationMs;
      return event;
    }
    case 'turn.text': {
      if (typeof raw.text !== 'string') return null;
      const text = raw.text.trim();
      return text && text.length <= MAX_TURN_TEXT_CHARS ? { type: 'turn.text', text } : null;
    }
    case 'ptt.up':
      return isFrameReleaseReason(raw.reason) ? { type: 'ptt.up', reason: raw.reason } : null;
    case 'settings.set': {
      const patch = parseSettingsPatch(raw.patch);
      return patch ? { type: 'settings.set', patch } : null;
    }
    case 'debug.state':
    case 'debug.scenePing':
    case 'demo.run':
    case 'debug.marks':
    case 'debug.sceneClose':
      return isTabId(raw.tabId) ? { type: raw.type, tabId: raw.tabId } : null;
    case 'debug.pageContext': {
      if (!isTabId(raw.tabId)) return null;
      if (raw.budget === undefined) return { type: 'debug.pageContext', tabId: raw.tabId };
      return raw.budget === 'full' || raw.budget === 'compact'
        ? { type: 'debug.pageContext', tabId: raw.tabId, budget: raw.budget } : null;
    }
    case 'scene.result': {
      if (!isSceneId(raw.scene_id) || !isCount(raw.attempt, 10) || raw.attempt < 1 || typeof raw.ok !== 'boolean') {
        return null;
      }
      const result: SwRequest = { type: 'scene.result', scene_id: raw.scene_id, attempt: raw.attempt, ok: raw.ok };
      if (typeof raw.error === 'string') result.error = raw.error.slice(0, 2_000);
      if (raw.unavailable === true) result.unavailable = true;
      return result;
    }
    case 'consent.get':
      return isTaskId(raw.taskId) ? { type: 'consent.get', taskId: raw.taskId } : null;
    case 'consent.decide':
      return isTaskId(raw.taskId) && (CONSENT_DECISIONS as readonly unknown[]).includes(raw.decision)
        ? { type: 'consent.decide', taskId: raw.taskId, decision: raw.decision as ConsentDecision } : null;
    case 'task.stop':
      return isTaskId(raw.taskId) ? { type: 'task.stop', taskId: raw.taskId } : null;
    case 'task.answer':
      return isTaskId(raw.taskId) && typeof raw.requestId === 'string' && raw.requestId.length <= 64
        && typeof raw.approved === 'boolean'
        ? { type: 'task.answer', taskId: raw.taskId, requestId: raw.requestId, approved: raw.approved } : null;
    case 'task.open':
      return isTaskId(raw.taskId) && typeof raw.url === 'string' && raw.url.length <= 4096
        ? { type: 'task.open', taskId: raw.taskId, url: raw.url } : null;
    case 'grants.list':
      return { type: 'grants.list' };
    case 'grants.clear':
      return { type: 'grants.clear' };
    case 'config.get':
      return { type: 'config.get' };
    case 'config.set': {
      const change = parseConfigChange(raw.change);
      return change ? { type: 'config.set', change } : null;
    }
    case 'grants.remove':
      return typeof raw.site === 'string' && raw.site.length <= 253 ? { type: 'grants.remove', site: raw.site } : null;
    case 'debug.taskAnswer':
      return isTabId(raw.tabId) && typeof raw.approved === 'boolean'
        ? { type: 'debug.taskAnswer', tabId: raw.tabId, approved: raw.approved } : null;
    case 'debug.sceneRender':
      return isTabId(raw.tabId) && isSceneKind(raw.kind) && typeof raw.code === 'string'
        && raw.code.length <= MAX_SCENE_CODE_CHARS
        ? { type: 'debug.sceneRender', tabId: raw.tabId, kind: raw.kind, code: raw.code,
            ...(typeof raw.title === 'string' ? { title: raw.title.slice(0, 120) } : {}) }
        : null;
    default:
      return null;
  }
}

const SCENE_ID = /^[A-Za-z0-9_-]{8,64}$/;

export function isTaskId(value: unknown): value is string {
  return typeof value === 'string' && SCENE_ID.test(value);
}

const TASK_EVENT_TYPES = ['task.consent', 'task.started', 'task.step', 'task.confirm.request', 'task.done'];

/** Shallow check of a task event the worker forwards (it built or parsed it already). */
export function parseTaskEvent(raw: unknown): TaskEvent | null {
  if (!isRecord(raw) || typeof raw.type !== 'string' || !TASK_EVENT_TYPES.includes(raw.type)) return null;
  if (!isTaskId(raw.task_id)) return null;
  if (raw.type === 'task.step' && (typeof raw.n !== 'number' || typeof raw.action_text !== 'string')) return null;
  if (raw.type === 'task.confirm.request' && (typeof raw.request_id !== 'string' || typeof raw.question !== 'string')) {
    return null;
  }
  if (raw.type === 'task.done' && (typeof raw.success !== 'boolean' || typeof raw.summary !== 'string')) return null;
  if (raw.type === 'task.consent' && (typeof raw.goal !== 'string' || !Array.isArray(raw.sites))) return null;
  return raw as unknown as TaskEvent;
}

export function isSceneId(value: unknown): value is string {
  return typeof value === 'string' && SCENE_ID.test(value);
}

/** Shallow check of a scene event the worker forwards (it parsed it from the backend already). */
export function parseSceneEvent(raw: unknown): SceneEvent | null {
  if (!isRecord(raw) || !isSceneId(raw.scene_id) || typeof raw.turn_id !== 'string') return null;
  switch (raw.type) {
    case 'scene.building':
      return isSceneKind(raw.kind) ? (raw as unknown as SceneEvent) : null;
    case 'scene.render':
      return isSceneKind(raw.kind) && typeof raw.code === 'string' && raw.code.length <= MAX_SCENE_CODE_CHARS
        ? (raw as unknown as SceneEvent) : null;
    case 'scene.failed':
      return typeof raw.error === 'string' ? (raw as unknown as SceneEvent) : null;
    default:
      return null;
  }
}

function isCount(value: unknown, max: number): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= max;
}

function isId(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 128;
}

function isLevel(value: unknown): value is number {
  return typeof value === 'number' && value >= 0 && value <= 1;
}

export function isPlaybackState(value: unknown): value is PlaybackState {
  return typeof value === 'string' && (PLAYBACK_STATES as readonly string[]).includes(value);
}

export function parseSnapshotBudget(raw: unknown): SnapshotBudget | null {
  if (!isRecord(raw)) return null;
  const { maxElements, maxTextChars, maxNameChars } = raw;
  if (!isCount(maxElements, 400) || !isCount(maxTextChars, 20_000) || !isCount(maxNameChars, 200)) return null;
  return { maxElements, maxTextChars, maxNameChars };
}

function parseImageSize(raw: unknown): ImageSize | null {
  if (!isRecord(raw) || !isCount(raw.width, 8192) || !isCount(raw.height, 8192)) return null;
  return raw.width > 0 && raw.height > 0 ? { width: raw.width, height: raw.height } : null;
}

/** Shallow check: the worker only forwards server events it already parsed. */
export function parseTabTurnEvent(raw: unknown): TabTurnEvent | null {
  if (!isRecord(raw) || typeof raw.type !== 'string') return null;
  if (!(TAB_TURN_EVENT_TYPES as readonly string[]).includes(raw.type)) return null;
  if (raw.type !== 'error' && typeof raw.turn_id !== 'string') return null;
  if (raw.type === 'visual.directive' && (!isRecord(raw.directive) || typeof raw.directive.kind !== 'string')) {
    return null;
  }
  return raw as unknown as TabTurnEvent;
}

export function parseTabMessage(raw: unknown): TabMessage | null {
  if (!isRecord(raw) || typeof raw.type !== 'string') return null;
  switch (raw.type) {
    case 'worker.started':
      return { type: 'worker.started' };
    case 'status.changed': {
      const status = raw.status;
      if (!isRecord(status) || !isConnState(status.state) || !isOrbState(status.orb)) return null;
      return { type: 'status.changed', status: { state: status.state, orb: status.orb } };
    }
    case 'orb.state':
      return isOrbState(raw.state) ? { type: 'orb.state', state: raw.state } : null;
    case 'ptt.state':
      return typeof raw.holding === 'boolean' ? { type: 'ptt.state', holding: raw.holding } : null;
    case 'orb.toggle':
    case 'prefs.changed':
    case 'debug.state':
    case 'debug.scenePing':
    case 'demo.run':
    case 'debug.sceneClose':
      return { type: raw.type };
    case 'turn.event': {
      const event = parseTabTurnEvent(raw.event);
      return event ? { type: 'turn.event', event } : null;
    }
    case 'context.snapshot': {
      const budget = parseSnapshotBudget(raw.budget);
      return budget && typeof raw.hideUi === 'boolean' ? { type: 'context.snapshot', budget, hideUi: raw.hideUi } : null;
    }
    case 'context.restore': {
      if (raw.image === null) return { type: 'context.restore', image: null };
      const image = parseImageSize(raw.image);
      return image ? { type: 'context.restore', image } : null;
    }
    case 'debug.marks': {
      const budget = parseSnapshotBudget(raw.budget);
      return budget ? { type: 'debug.marks', budget } : null;
    }
    case 'voice.turn':
      return isId(raw.turnId) ? { type: 'voice.turn', turnId: raw.turnId } : null;
    case 'voice.error':
      return typeof raw.message === 'string' ? { type: 'voice.error', message: raw.message.slice(0, 300) } : null;
    case 'voice.level':
      return (raw.source === 'mic' || raw.source === 'speech') && isLevel(raw.level)
        ? { type: 'voice.level', source: raw.source, level: raw.level }
        : null;
    case 'speech.playback':
      return isId(raw.turnId) && isCount(raw.seq, 100_000) && isPlaybackState(raw.state)
        && typeof raw.inMs === 'number'
        ? { type: 'speech.playback', turnId: raw.turnId, seq: raw.seq, state: raw.state, inMs: raw.inMs }
        : null;
    case 'scene.event': {
      const event = parseSceneEvent(raw.event);
      return event ? { type: 'scene.event', event } : null;
    }
    case 'debug.sceneRender':
      return isSceneKind(raw.kind) && typeof raw.code === 'string' && raw.code.length <= MAX_SCENE_CODE_CHARS
        && typeof raw.title === 'string'
        ? { type: 'debug.sceneRender', kind: raw.kind, code: raw.code, title: raw.title.slice(0, 120) }
        : null;
    case 'task.event': {
      const event = parseTaskEvent(raw.event);
      return event ? { type: 'task.event', event } : null;
    }
    case 'context.storage':
      return { type: 'context.storage' };
    case 'debug.taskAnswer':
      return typeof raw.approved === 'boolean' ? { type: 'debug.taskAnswer', approved: raw.approved } : null;
    default:
      return null;
  }
}

export function parseOffscreenRequest(raw: unknown): OffscreenRequest | null {
  if (!isRecord(raw) || raw.target !== 'offscreen') return null;
  const target = 'offscreen' as const;
  switch (raw.type) {
    case 'ping':
    case 'mic.probe':
    case 'rec.cancel':
    case 'play.stop':
      return { target, type: raw.type };
    case 'rec.start':
    case 'rec.stop':
      return isId(raw.turnId) ? { target, type: raw.type, turnId: raw.turnId } : null;
    case 'play.segment':
      return isId(raw.turnId) && isCount(raw.seq, 100_000) && typeof raw.mime === 'string'
        && (raw.sampleRate === null || isCount(raw.sampleRate, 384_000)) && typeof raw.text === 'string'
        ? { target, type: 'play.segment', turnId: raw.turnId, seq: raw.seq, mime: raw.mime,
            sampleRate: raw.sampleRate as number | null, text: raw.text }
        : null;
    case 'play.audio':
      return isId(raw.turnId) && isCount(raw.seq, 100_000) && typeof raw.data === 'string'
        ? { target, type: 'play.audio', turnId: raw.turnId, seq: raw.seq, data: raw.data }
        : null;
    case 'play.end':
      return isId(raw.turnId) && isCount(raw.seq, 100_000)
        ? { target, type: 'play.end', turnId: raw.turnId, seq: raw.seq }
        : null;
    default:
      return null;
  }
}

export function parseDebugTurnCommand(raw: unknown): DebugTurnCommand | null {
  if (!isRecord(raw)) return null;
  if (raw.type === 'turn.cancel') return { type: 'turn.cancel' };
  if (raw.type !== 'turn.start' || typeof raw.text !== 'string') return null;
  const text = raw.text.trim();
  return text && text.length <= MAX_DEBUG_TURN_CHARS ? { type: 'turn.start', text } : null;
}
