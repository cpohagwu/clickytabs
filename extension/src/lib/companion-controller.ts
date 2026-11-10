/**
 * The page-side half of the companion: runs HUD text turns through the service worker, plays the scripted overlay
 * demo, applies visual directives, and keeps the store (orb activity, captions, synthetic voice level) in sync.
 *
 * Pure enough to unit-test: extension APIs come in through `request`, the view (canvas renderer + orb position)
 * attaches once the Svelte app has mounted.
 */
import { get, type Writable } from 'svelte/store';

import { type CompanionState, displayOrb } from './companion';
import { estimateSpeechMs } from './audio/pcm';
import type {
  Ack,
  ImageSize,
  LayoutProbe,
  OrbState,
  PlaybackState,
  SwRequest,
  TabTurnEvent,
  TurnTextResponse,
} from './messages';
import { imageToPage, randomSentinel, type Rgb } from './page-context/capture';
import { type PageSnapshot, type SnapshotBudget, snapshotPage } from './page-context/snapshot';
import type { CompanionMode } from './prefs';
import type { TimedDirective, Viewport } from './protocol.gen';
import { type DemoStep, planDemo } from './overlay/demo';
import { applyDirective, type Directive, type DirectiveHost } from './overlay/directives';
import type { Point } from './overlay/geometry';
import type { OverlayRenderer } from './overlay/renderer';
import { offsetForChar, scheduleSegment, Timeline, type TimelineTimers } from './overlay/scheduler';
import { ElementRegistry } from './overlay/targets';
import { buildTimeline, type ClientMarks, type TurnTimeline } from './timeline';

export interface CompanionView {
  renderer: OverlayRenderer;
  /** Orb center in viewport px: where pointer flights start. */
  origin(): Point;
  /** Re-promote the companion layer above page dialogs/popovers before drawing. */
  raise(): void;
  /**
   * Hide (or show again) the whole companion, synchronously, so a screenshot shows only the page; while hidden, show
   * the capture sentinel in `sentinel`'s colour.
   */
  setCapturing?(on: boolean, sentinel?: Rgb): void;
  probe?(): LayoutProbe;
}

export interface ControllerDeps {
  store: Writable<CompanionState>;
  request<T>(message: SwRequest): Promise<T | undefined>;
  document?: Document;
  viewport?(): { width: number; height: number };
  reducedMotion?(): boolean;
  timers?: TimelineTimers;
  log?(message: string, error?: unknown): void;
}

const MAX_CAPTION_CHARS = 4000;
const ERROR_LINGER_MS = 5_000;
const LEVEL_TICK_MS = 90;
const STEP_GAP_MS = 380;
const DEMO_PROMPT = 'Show me what you can do.';
/** The companion reappears on its own if the worker never says the capture is over. */
const CAPTURE_RESTORE_MS = 2_000;

const TURN_ACTIVITY: Partial<Record<string, OrbState>> = {
  listening: 'listening',
  transcribing: 'thinking',
  thinking: 'thinking',
  speaking: 'speaking',
  acting: 'acting',
};

/** A voice turn ends when its playback is idle; this long after turn.done it ends regardless. */
const VOICE_DRAIN_MS = 20_000;

interface VoiceSegment {
  text: string;
  directives: TimedDirective[];
  durationMs?: number;
  charTimes?: number[];
}

/** The page side of a push-to-talk turn: captions and pointers follow what is audible, not what was generated. */
interface VoiceTurn {
  turnId: string;
  segments: Map<number, VoiceSegment>;
  text: string;            // everything the brain said (shown at once if no audio plays)
  revealed: string;        // captions so far
  played: boolean;
  /** Segments whose audio has started: the turn isn't over until every announced one has. */
  started: Set<number>;
  idle: boolean;
  done: boolean;
  timeline: Timeline;
  drain: ReturnType<typeof setTimeout> | null;
}

/**
 * Every sentence the backend announced has started playing. `idle` alone isn't the end: it only means the player
 * ran out of what it had, and with a fast `turn.done` (or slow speech synthesis) the last sentences can still be on
 * their way through the worker. Ending then would play them with no captions and no pointer.
 */
function allStarted(voice: VoiceTurn): boolean {
  for (const seq of voice.segments.keys()) if (!voice.started.has(seq)) return false;
  return true;
}

const DONE_MESSAGES: Record<string, string> = {
  error: 'The turn failed.',
  refusal: 'The model declined to answer that.',
  max_tokens: 'The reply was cut off (token limit).',
};

export class CompanionController {
  readonly registry = new ElementRegistry();
  private view: CompanionView | null = null;
  private demo: Timeline | null = null;
  private awaitingTurn = false;
  private levelTimer: ReturnType<typeof setInterval> | null = null;
  private errorTimer: ReturnType<typeof setTimeout> | null = null;
  private restoreTimer: ReturnType<typeof setTimeout> | null = null;
  private voice: VoiceTurn | null = null;
  /** The current turn's latency marks: a voice turn starts counting when the key is released. */
  private timing: { kind: TurnTimeline['kind']; start: number | null; marks: ClientMarks;
    metrics?: Record<string, number | undefined> } | null = null;
  /** The latest directive applied and what it targeted (debug.state, e2e). */
  lastDirective: { kind: string; target: string | null } | null = null;
  /** Geometry of the latest turn's capture: maps the brain's screenshot coordinates back onto the page. */
  private capture: { viewport: Viewport; image: ImageSize | null } | null = null;

  constructor(private readonly deps: ControllerDeps) {}

  // ------------------------------------------------------------------------------------------------ view

  attachView(view: CompanionView): void {
    this.view = view;
  }

  detachView(): void {
    this.view = null;
  }

  get annotations(): number {
    return this.view?.renderer.count ?? 0;
  }

  get animating(): boolean {
    return this.view?.renderer.animating ?? false;
  }

  layout(): LayoutProbe | null {
    return this.view?.probe?.() ?? null;
  }

  toggleHud(open?: boolean): void {
    this.deps.store.update((s) => ({ ...s, hudOpen: open ?? !s.hudOpen }));
  }

  setMode(mode: CompanionMode): void {
    this.deps.store.update((s) => ({ ...s, mode }));
  }

  // ------------------------------------------------------------------------------------------------ page context

  /**
   * DOM snapshot for the worker (which adds the screenshot). Element ids restart at e1, so the brain's directives
   * refer to this snapshot. With `hideUi` the companion disappears until `restoreAfterCapture` (or a timeout).
   */
  snapshotForTurn(budget: SnapshotBudget, hideUi: boolean): PageSnapshot {
    const sentinel = hideUi && this.view?.setCapturing ? randomSentinel() : undefined;
    if (sentinel) {
      this.view?.setCapturing?.(true, sentinel);
      if (this.restoreTimer !== null) clearTimeout(this.restoreTimer);
      this.restoreTimer = setTimeout(() => this.restoreAfterCapture(null), CAPTURE_RESTORE_MS);
    }
    const snapshot = snapshotPage(this.deps.document ?? document, this.registry, budget);
    this.capture = { viewport: snapshot.viewport, image: null };
    return sentinel ? { ...snapshot, sentinel } : snapshot;
  }

  restoreAfterCapture(image: ImageSize | null): void {
    if (this.restoreTimer !== null) clearTimeout(this.restoreTimer);
    this.restoreTimer = null;
    this.view?.setCapturing?.(false);
    if (this.capture && image) this.capture.image = image;
  }

  /** Debug: outline what the brain would get as marks (ids as labels). */
  showMarks(budget: SnapshotBudget): number {
    this.halt();
    const view = this.view;
    const snapshot = snapshotPage(this.deps.document ?? document, this.registry, budget);
    this.capture = { viewport: snapshot.viewport, image: null };
    if (!view) return 0;
    view.raise();
    for (const mark of snapshot.elements) {
      const element = this.registry.get(mark.id);
      if (element) view.renderer.mark({ kind: 'element', element }, 'box', mark.id);
    }
    return snapshot.elements.length;
  }

  // ------------------------------------------------------------------------------------------------ demo

  /** Scripted tour of the current page through the real directive path; needs no backend. */
  async runDemo(): Promise<void> {
    if (this.halt()) void this.deps.request<Ack>({ type: 'turn.stop' });
    const view = this.view;
    if (!view) return;
    const host = this.host(view);
    const steps = planDemo(this.deps.document ?? document, this.registry, this.viewport());
    const timeline = new Timeline(this.deps.timers);
    this.demo = timeline;
    view.raise();
    this.patch({ busy: true, error: null, userText: DEMO_PROMPT, caption: '', activity: 'speaking', turnId: null });
    this.startLevel();
    for (const step of steps) {
      this.playStep(step, host, timeline);
      await timeline.sleep(step.durationMs + STEP_GAP_MS); // never resolves once cancelled
    }
    if (this.demo === timeline) {
      this.demo = null;
      this.finish('idle');
    }
  }

  private playStep(step: DemoStep, host: DirectiveHost, timeline: Timeline): void {
    const before = get(this.deps.store).caption;
    const prefix = before ? `${before} ` : '';
    for (const word of step.text.matchAll(/\S+\s*/g)) {
      const end = word.index + word[0].length;
      timeline.at(offsetForChar(word.index, step.text.length, step.durationMs), () =>
        this.patch({ caption: (prefix + step.text.slice(0, end).trimEnd()).slice(-MAX_CAPTION_CHARS) }));
    }
    const items = step.directives.map(({ at_char, directive }) => ({ at_char, item: directive }));
    for (const { delayMs, item } of scheduleSegment(step.text, step.durationMs, items)) {
      timeline.at(delayMs, () => this.apply(item, host));
    }
  }

  // ------------------------------------------------------------------------------------------------ turns

  async sendText(text: string): Promise<void> {
    const trimmed = text.trim();
    if (!trimmed) return;
    this.halt(); // the worker replaces this tab's previous turn itself
    this.timing = { kind: 'text', start: Date.now(), marks: {} };
    this.awaitingTurn = true;
    this.patch({ userText: trimmed, caption: '', error: null, busy: true, activity: 'thinking', turnId: null });
    this.view?.raise();
    const reply = await this.deps.request<TurnTextResponse>({ type: 'turn.text', text: trimmed });
    const wanted = this.awaitingTurn;
    this.awaitingTurn = false;
    if (!reply?.ok) {
      if (wanted) this.fail(reply?.error ?? 'The extension is restarting; try again.');
      return;
    }
    const state = get(this.deps.store);
    if (wanted && state.busy && (state.turnId === null || state.turnId === reply.turn_id)) {
      this.patch({ turnId: reply.turn_id });
    } else if (!wanted) {
      await this.deps.request<Ack>({ type: 'turn.stop' }); // stopped while the worker was starting it
    }
  }

  // ------------------------------------------------------------------------------------------------ voice

  /** The worker started a push-to-talk turn in this tab. */
  beginVoiceTurn(turnId: string): void {
    this.halt();
    this.voice = { turnId, segments: new Map(), text: '', revealed: '', played: false, started: new Set(), idle: false,
      done: false, timeline: new Timeline(this.deps.timers), drain: null };
    this.timing = { kind: 'voice', start: null, marks: {} };
    this.view?.raise();
    this.patch({ turnId, userText: '', caption: '', error: null, busy: true, activity: 'listening' });
  }

  /** The key was released: from now on the orb shows the backend's progress. */
  onPttReleased(): void {
    if (this.timing?.kind === 'voice' && this.timing.start === null) this.timing.start = Date.now();
    if (this.voice && get(this.deps.store).activity === 'listening') this.patch({ activity: 'thinking' });
  }

  /** Record a moment of the current turn (ms after its start) and refresh the latency timeline. */
  private mark(name: keyof ClientMarks, at = Date.now(), metrics?: Record<string, number | undefined> | null): void {
    const timing = this.timing;
    if (!timing || timing.start === null) return;
    if (timing.marks[name] === undefined) timing.marks[name] = at - timing.start;
    if (metrics) timing.metrics = metrics;
    const timeline = buildTimeline(timing.kind, timing.marks, timing.metrics);
    if (timeline) this.patch({ timeline });
  }

  voiceError(message: string): void {
    this.endVoice();
    this.fail(message);
  }

  /**
   * A new service worker started: the old one's socket closed with it, so a turn in flight will never finish.
   * End it with a message instead of thinking forever.
   */
  workerRestarted(): void {
    const state = get(this.deps.store);
    if (state.holding) this.patch({ holding: false });
    if (!state.busy && this.voice === null) return;
    this.endVoice();
    this.fail('The connection to the backend was interrupted. Please ask again.');
  }

  /** Mic level while listening, speech level while answering. */
  onLevel(source: 'mic' | 'speech', level: number): void {
    if (this.deps.reducedMotion?.()) return;
    if (source === 'mic' ? displayOrb(get(this.deps.store)) === 'listening' : this.voice !== null) this.patch({ level });
  }

  onPlayback(turnId: string, seq: number, state: PlaybackState, inMs: number): void {
    const voice = this.voice;
    if (!voice || voice.turnId !== turnId) return;
    if (state === 'started') {
      if (!voice.played) this.mark('heard', Date.now() + inMs);
      voice.played = true;
      voice.started.add(seq);
      voice.idle = false;
      const segment = voice.segments.get(seq);
      voice.timeline.at(inMs, () => this.patch({ activity: 'speaking' }));
      if (segment) this.speakSegment(voice, segment, inMs);
    } else if (state === 'idle') {
      voice.idle = true;
      if (voice.done && allStarted(voice)) this.completeVoice();
      else this.patch({ activity: 'thinking', level: 0 });       // more sentences are coming
    } else if (state === 'interrupted') {
      voice.timeline.cancel();
    }
  }

  private speakSegment(voice: VoiceTurn, segment: VoiceSegment, inMs: number): void {
    const duration = segment.durationMs || estimateSpeechMs(segment.text);
    const prefix = voice.revealed ? `${voice.revealed} ` : '';
    for (const word of segment.text.matchAll(/\S+\s*/g)) {
      const end = word.index + word[0].length;
      const at = segment.charTimes?.[word.index] ?? offsetForChar(word.index, segment.text.length, duration);
      voice.timeline.at(inMs + at, () =>
        this.patch({ caption: (prefix + segment.text.slice(0, end).trimEnd()).slice(-MAX_CAPTION_CHARS) }));
    }
    voice.revealed = (prefix + segment.text).slice(-MAX_CAPTION_CHARS);
    if (this.view && segment.directives.length) {
      const host = this.host(this.view);
      const items = segment.directives.map(({ at_char, directive }) => ({ at_char, item: directive }));
      for (const { delayMs, item } of scheduleSegment(segment.text, duration, items, segment.charTimes)) {
        voice.timeline.at(inMs + delayMs, () => this.apply(item, host));
      }
    }
  }

  private onVoiceEvent(voice: VoiceTurn, event: TabTurnEvent): boolean {
    switch (event.type) {
      case 'transcript.partial':
      case 'transcript.final':
        this.patch({ userText: event.text });
        return true;
      case 'speech.segment':
        voice.segments.set(event.seq, { text: event.text, directives: event.directives ?? [] });
        return true;
      case 'speech.segment.end': {
        const segment = voice.segments.get(event.seq);
        if (segment) {
          segment.durationMs = event.duration_ms;
          if (event.char_times_ms) segment.charTimes = event.char_times_ms;
        }
        return true;
      }
      case 'assistant.text.delta':
        voice.text += event.delta;                   // captions follow the audio (speakSegment)
        return true;
      case 'turn.state':
        if (event.state !== 'speaking' && event.state !== 'listening') {
          this.patch({ activity: TURN_ACTIVITY[event.state] ?? 'thinking' });
        }
        return true;
      case 'turn.done':
        voice.done = true;
        if (event.reason === 'end') this.mark('done', Date.now(), event.metrics);
        if (event.reason !== 'end') {
          this.endVoice();
          if (event.reason === 'cancelled') this.finish('idle');
          else this.fail(event.detail === 'no_speech' ? (get(this.deps.store).error ?? 'I didn\'t catch that.')
            : get(this.deps.store).error ?? DONE_MESSAGES[event.reason] ?? 'The turn failed.');
          return true;
        }
        if (!voice.played) {
          this.patch({ caption: voice.text.trim().slice(-MAX_CAPTION_CHARS) });   // no audio: show the answer
          this.completeVoice();
        } else if (voice.idle && allStarted(voice)) {
          this.completeVoice();
        } else {                                     // sentences still on their way to the player
          voice.drain = setTimeout(() => this.completeVoice(), VOICE_DRAIN_MS);
        }
        return true;
      default:
        return false;
    }
  }

  private completeVoice(): void {
    const voice = this.voice;
    if (!voice) return;
    // Let the last scheduled captions land before the turn ends.
    voice.timeline.at(0, () => {
      if (this.voice !== voice) return;
      this.endVoice(false);
      this.finish('idle');
    });
  }

  private endVoice(cancelTimeline = true): void {
    const voice = this.voice;
    if (!voice) return;
    if (cancelTimeline) voice.timeline.cancel();
    if (voice.drain !== null) clearTimeout(voice.drain);
    this.voice = null;
  }

  onTurnEvent(event: TabTurnEvent): void {
    const state = get(this.deps.store);
    const turnId = event.turn_id ?? null;
    if (turnId === null || turnId !== state.turnId) {
      // Events can overtake the turn.text reply: adopt the first turn id while waiting for it.
      if (!(this.awaitingTurn && state.turnId === null && turnId !== null)) return;
      this.patch({ turnId });
    }
    if (this.voice && this.voice.turnId === turnId && this.onVoiceEvent(this.voice, event)) return;
    switch (event.type) {
      case 'turn.state': {
        const activity = TURN_ACTIVITY[event.state];
        if (!activity) return;
        this.patch({ activity });
        if (activity === 'speaking') this.startLevel();
        return;
      }
      case 'assistant.text.delta':
        this.mark('firstText');
        this.patch({ caption: (get(this.deps.store).caption + event.delta).slice(-MAX_CAPTION_CHARS), activity: 'speaking' });
        this.startLevel();
        return;
      case 'transcript.partial':
      case 'transcript.final':
      case 'speech.segment':
      case 'speech.segment.end':
        return;
      case 'visual.directive':
        if (this.view) this.apply(event.directive, this.host(this.view));
        return;
      case 'error':
        this.patch({ error: event.message });
        return;
      case 'turn.done':
        this.awaitingTurn = false;
        if (event.reason === 'end') this.mark('done');
        if (event.reason === 'end' || event.reason === 'cancelled') this.finish('idle');
        else this.fail(event.detail ?? get(this.deps.store).error ?? DONE_MESSAGES[event.reason] ?? 'The turn failed.');
        return;
    }
  }

  /** Stop the demo or the current turn and clear the overlay. */
  async stop(): Promise<void> {
    if (this.halt()) await this.deps.request<Ack>({ type: 'turn.stop' });
  }

  destroy(): void {
    this.endVoice();
    this.demo?.cancel();
    this.demo = null;
    this.stopLevel();
    if (this.errorTimer !== null) clearTimeout(this.errorTimer);
    if (this.restoreTimer !== null) clearTimeout(this.restoreTimer);
    this.view = null;
  }

  // ------------------------------------------------------------------------------------------------ internals

  /** Synchronous part of stop(): returns whether a backend turn was running (and must be cancelled). */
  private halt(): boolean {
    const state = get(this.deps.store);
    this.endVoice();
    this.demo?.cancel();
    this.demo = null;
    const hadTurn = state.turnId !== null || this.awaitingTurn;
    this.awaitingTurn = false;
    this.view?.renderer.clear();
    if (state.busy || state.activity !== 'idle') this.finish('idle');
    return hadTurn;
  }

  private patch(values: Partial<CompanionState>): void {
    this.deps.store.update((s) => ({ ...s, ...values }));
  }

  private viewport(): { width: number; height: number } {
    return this.deps.viewport?.() ?? { width: innerWidth, height: innerHeight };
  }

  private host(view: CompanionView): DirectiveHost {
    return {
      renderer: view.renderer,
      registry: this.registry,
      origin: () => view.origin(),
      toPage: (x, y) =>
        this.capture ? imageToPage(x, y, this.capture.viewport, this.capture.image) : { x: x + scrollX, y: y + scrollY },
    };
  }

  private apply(directive: Directive, host: DirectiveHost): void {
    this.lastDirective = { kind: directive.kind, target: this.describeTarget(directive) };
    applyDirective(directive, host).catch((error: unknown) => this.deps.log?.('directive failed', error));
  }

  /** Debug/e2e: what a directive aims at - the element's accessible text, or the page point. */
  private describeTarget(directive: Directive): string | null {
    const ref = directive.kind === 'arrow' ? directive.target
      : 'to' in directive ? directive.to ?? null : null;
    if (ref) {
      const element = this.registry.get(ref);
      if (!element) return ref;
      const text = element.getAttribute('aria-label') ?? element.textContent ?? '';
      return text.replace(/\s+/g, ' ').trim().slice(0, 80) || ref;
    }
    return directive.kind === 'point' && directive.x != null && directive.y != null
      ? `${Math.round(directive.x)},${Math.round(directive.y)}` : null;
  }

  private finish(activity: OrbState): void {
    this.stopLevel();
    this.patch({ busy: false, turnId: null, activity, level: 0 });
  }

  private fail(message: string): void {
    this.finish('error');
    this.patch({ error: message });
    if (this.errorTimer !== null) clearTimeout(this.errorTimer);
    this.errorTimer = setTimeout(() => {
      this.errorTimer = null;
      if (get(this.deps.store).activity === 'error') this.patch({ activity: 'idle' });
    }, ERROR_LINGER_MS);
  }

  /** Speech-like envelope for the orb while "speaking", until the real playback level arrives. */
  private startLevel(): void {
    if (this.levelTimer !== null || this.deps.reducedMotion?.()) return;
    const born = Date.now();
    this.levelTimer = setInterval(() => {
      if (displayOrb(get(this.deps.store)) !== 'speaking') {
        this.stopLevel();
        this.patch({ level: 0 });
        return;
      }
      const t = (Date.now() - born) / 1000;
      const syllables = Math.abs(Math.sin(t * 9.1)) * 0.55 + Math.abs(Math.sin(t * 3.7 + 1)) * 0.3;
      this.patch({ level: Math.min(1, 0.12 + syllables * (0.75 + Math.random() * 0.25)) });
    }, LEVEL_TICK_MS);
  }

  private stopLevel(): void {
    if (this.levelTimer !== null) clearInterval(this.levelTimer);
    this.levelTimer = null;
  }
}
