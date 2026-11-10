/**
 * The service worker's single backend WebSocket: reconnect policy (pure functions) and the connection manager.
 *
 * Lifecycle: `connecting` → (open) → hello → (welcome) → `connected`, with an app-level ping every 20 s, which also
 * keeps the MV3 worker alive. Failures back off from 1 s to a 25 s cap with jitter. The worker can be killed while
 * it waits, so the caller additionally keeps a 30 s `chrome.alarms` alarm while the state wants one
 * ({@link wantsReconnectAlarm}). Close 4401 is terminal (`unauthorized`) until the token or URL changes; a
 * connection that fails before the handshake completes is followed by a `/healthz` probe to tell "backend down"
 * from "handshake rejected (extension ID not allowed?)".
 *
 * The manager has no extension-API dependencies: everything environmental is injected (see SocketManagerDeps).
 */
import type { ConnInfo, ConnState } from './conn';
import type { Logger } from './log';
import type { ClientEvent, ServerEvent } from './protocol.gen';
import { checkBackendUrl } from './url-policy';

// ---------------------------------------------------------------------------------------------- policy

export const PING_INTERVAL_MS = 20_000;
export const BACKOFF_BASE_MS = 1_000;
export const BACKOFF_CAP_MS = 25_000;
export const BACKOFF_JITTER = 0.2;
export const CONNECT_TIMEOUT_MS = 10_000;
export const WELCOME_TIMEOUT_MS = 10_000;
/** No frame at all for this long (2.5 ping periods) means the connection is dead even if TCP has not noticed. */
export const SILENCE_TIMEOUT_MS = 50_000;
export const RECONNECT_ALARM = 'cc-reconnect';
export const RECONNECT_ALARM_PERIOD_MIN = 0.5;

export const CLOSE_NORMAL = 1000;
export const CLOSE_ABNORMAL = 1006;
export const CLOSE_PROTOCOL_ERROR = 4400;
export const CLOSE_UNAUTHORIZED = 4401;
export const CLOSE_FORBIDDEN = 4403;

/** Same rule as the backend's `hello.conversation_id`; anything else is never sent. */
export const CONVERSATION_ID = /^[A-Za-z0-9_-]{8,64}$/;
export const CONVERSATION_KEY = 'conversation';

/** Exponential backoff: 1 s, 2 s, 4 s … capped at 25 s, with ±20 % jitter that never exceeds the cap. */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const exponent = Math.min(Math.max(Math.floor(attempt), 0), 16);
  const base = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** exponent);
  const factor = 1 - BACKOFF_JITTER + 2 * BACKOFF_JITTER * random();
  return Math.round(Math.min(BACKOFF_CAP_MS, base * factor));
}

/** Where a connection attempt was when it ended. */
export type ConnectPhase = 'connecting' | 'handshake' | 'open';

/**
 * - `unauthorized`: terminal until the credentials change.
 * - `rejected`: the server explicitly refused this client; keep retrying (its allow-list may change).
 * - `probe`: failed before the handshake completed; ask `/healthz` whether the backend is up at all.
 * - `retry`: anything else; back off and reconnect.
 */
export type CloseOutcome = 'unauthorized' | 'rejected' | 'probe' | 'retry';

export function classifyClose(code: number, phase: ConnectPhase): CloseOutcome {
  if (code === CLOSE_UNAUTHORIZED) return 'unauthorized';
  if (code === CLOSE_FORBIDDEN) return 'rejected';
  if (phase === 'connecting' || (phase === 'handshake' && code === CLOSE_ABNORMAL)) return 'probe';
  return 'retry';
}

/** The 30 s reconnect alarm exists exactly while a (re)connection is pending. */
/**
 * The 30 s alarm runs while a connection is pending and, as a watchdog, while connected: if Chrome stops the
 * worker (and the socket with it), the alarm starts a new one within 30 s, which reconnects and tells the tabs.
 * Only the states that need the user (unpaired, unauthorized) go without.
 */
export function wantsReconnectAlarm(state: ConnState): boolean {
  return state === 'connecting' || state === 'disconnected' || state === 'rejected' || state === 'connected';
}

/** Short, non-reversible id of the credentials, used to remember a 4401 across worker restarts. */
export async function credentialFingerprint(url: string, token: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`${url}\n${token}`));
  return [...new Uint8Array(digest)]
    .slice(0, 16)
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

function describeClose(code: number, reason: string): string {
  if (reason) return reason;
  if (code === CLOSE_PROTOCOL_ERROR) return 'Protocol error.';
  if (code === CLOSE_NORMAL || code === 1001) return 'The backend closed the connection.';
  return `Connection lost (code ${code}).`;
}

// ---------------------------------------------------------------------------------------------- manager

export interface SocketHandlers {
  onOpen(): void;
  onMessage(data: string): void;
  onClose(code: number, reason: string): void;
}

export interface SocketLike {
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

/** Creates and connects a socket. May throw synchronously (treated as a failed connection). */
export type SocketFactory = (url: string, handlers: SocketHandlers) => SocketLike;

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
  setInterval(fn: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
}

export interface SocketManagerDeps {
  readCredentials(): Promise<{ backendUrl: string; token: string }>;
  createSocket: SocketFactory;
  /** Resolves true when `GET healthUrl` answers 2xx. Must not throw. */
  probeHealth(healthUrl: string): Promise<boolean>;
  /** Persist + broadcast the new state. Called synchronously on every transition, in order. */
  publish(info: ConnInfo): void;
  /** Persisted fingerprint of credentials that got a 4401 (see {@link credentialFingerprint}). */
  readBlocked(): Promise<string | null>;
  writeBlocked(fingerprint: string | null): Promise<void>;
  onServerEvent(event: ServerEvent): void;
  /** A welcomed session ended (any reason). */
  onSessionEnded?(): void;
  /**
   * The conversation to continue: read before every hello, written from `welcome` (the backend may have started
   * a new one). Kept in chrome.storage.session, so it survives service-worker restarts but not a browser restart.
   */
  conversation?: { read(): Promise<string | null>; write(id: string): void };
  extensionId: string;
  clientVersion: string;
  log: Logger;
  timers?: Timers;
  random?: () => number;
  now?: () => number;
}

interface Attempt {
  socket: SocketLike;
  phase: ConnectPhase;
  healthUrl: string;
  fingerprint: string;
}

const defaultTimers: Timers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
  setInterval: (fn, ms) => globalThis.setInterval(fn, ms),
  clearInterval: (handle) => globalThis.clearInterval(handle as ReturnType<typeof setInterval>),
};

function isServerEvent(value: unknown): value is ServerEvent {
  return typeof value === 'object' && value !== null && typeof (value as { type?: unknown }).type === 'string';
}

export class SocketManager {
  private readonly timers: Timers;
  private readonly random: () => number;
  private readonly now: () => number;
  private info: ConnInfo;
  private current: Attempt | null = null;
  private generation = 0;
  private failures = 0;
  private terminal = false;
  private inflight: Promise<void> | null = null;
  private settle: (() => void) | null = null;
  private retryTimer: unknown = null;
  private phaseTimer: unknown = null;
  private pingTimer: unknown = null;
  private lastFrameAt = 0;

  constructor(private readonly deps: SocketManagerDeps) {
    this.timers = deps.timers ?? defaultTimers;
    this.random = deps.random ?? Math.random;
    this.now = deps.now ?? Date.now;
    this.info = { state: 'connecting', updatedAt: this.now() };
  }

  get conn(): ConnInfo {
    return this.info;
  }

  get isOpen(): boolean {
    return this.current?.phase === 'open';
  }

  /**
   * Idempotent trigger (startup, alarm, any message…). Returns the single in-flight attempt, if any. Does nothing
   * while connected, while waiting out a backoff delay, or in a terminal state.
   */
  ensureConnected(trigger: string): Promise<void> {
    if (this.inflight) return this.inflight;
    if (this.current || this.retryTimer !== null || this.terminal) return Promise.resolve();
    return this.connect(trigger);
  }

  /** Credentials changed: forget terminal state and backoff, drop the current socket and connect again. */
  async reset(trigger: string): Promise<void> {
    this.terminal = false;
    this.failures = 0;
    this.clearRetry();
    const previous = this.current;
    this.generation += 1; // invalidates every pending step of the previous attempt
    if (previous) {
      this.teardown();
      try {
        previous.socket.close(CLOSE_NORMAL, 'settings changed');
      } catch {
        // already closed
      }
      if (previous.phase === 'open') this.deps.onSessionEnded?.();
    }
    this.finishAttempt();
    await this.deps.writeBlocked(null);
    return this.connect(trigger);
  }

  /** Sends an event on the welcomed session. Returns false when not connected. */
  send(event: ClientEvent): boolean {
    if (this.current?.phase !== 'open') return false;
    return this.sendRaw(event);
  }

  // ----------------------------------------------------------------------------------------- internals

  private connect(trigger: string): Promise<void> {
    const generation = ++this.generation;
    const attempt = new Promise<void>((resolve) => {
      this.settle = resolve;
    });
    this.inflight = attempt;
    void attempt.then(() => {
      if (this.inflight === attempt) this.inflight = null;
    });
    this.deps.log.debug('connect', { trigger, attempt: this.failures });
    void this.run(generation).catch((error: unknown) => {
      if (generation !== this.generation) return;
      this.deps.log.error('connect failed unexpectedly', error);
      this.scheduleRetry({ state: 'disconnected', detail: 'Internal error while connecting.' });
    });
    return attempt;
  }

  private async run(generation: number): Promise<void> {
    this.publish({ state: 'connecting' });
    const { backendUrl, token } = await this.deps.readCredentials();
    if (generation !== this.generation) return;
    if (!token) {
      this.enterTerminal({ state: 'unpaired', detail: 'Paste the pairing token from `clicky token show`.' });
      return;
    }
    const check = checkBackendUrl(backendUrl);
    if (!check.ok) {
      this.enterTerminal({ state: 'unpaired', detail: check.error });
      return;
    }
    const fingerprint = await credentialFingerprint(check.url, token);
    const blocked = await this.deps.readBlocked();
    const conversationId = (await this.deps.conversation?.read().catch(() => null)) ?? null;
    if (generation !== this.generation) return;
    if (blocked === fingerprint) {
      this.enterTerminal({
        state: 'unauthorized',
        closeCode: CLOSE_UNAUTHORIZED,
        detail: 'The backend rejected this token. Re-pair to continue.',
      });
      return;
    }
    this.open(check.url, check.healthUrl, token, fingerprint, conversationId);
  }

  private open(url: string, healthUrl: string, token: string, fingerprint: string,
    conversationId: string | null): void {
    let socket: SocketLike;
    const isCurrent = (): boolean => this.current !== null && this.current.socket === socket;
    try {
      socket = this.deps.createSocket(url, {
        onOpen: () => {
          if (isCurrent()) this.handleOpen(token, conversationId);
        },
        onMessage: (data) => {
          if (isCurrent()) this.handleMessage(data);
        },
        onClose: (code, reason) => {
          if (isCurrent()) this.handleClose(code, reason);
        },
      });
    } catch (error) {
      this.deps.log.warn('could not create the WebSocket', error);
      void this.afterFailure(this.generation, 'probe', CLOSE_ABNORMAL, '', healthUrl);
      return;
    }
    this.current = { socket, phase: 'connecting', healthUrl, fingerprint };
    this.armPhaseTimer(CONNECT_TIMEOUT_MS, 'Timed out connecting to the backend.');
  }

  private handleOpen(token: string, conversationId: string | null): void {
    const attempt = this.current;
    if (!attempt) return;
    attempt.phase = 'handshake';
    this.lastFrameAt = this.now();
    this.sendRaw({
      type: 'hello',
      token,
      client_version: this.deps.clientVersion,
      extension_id: this.deps.extensionId,
      features: [],
      ...(conversationId && CONVERSATION_ID.test(conversationId) ? { conversation_id: conversationId } : {}),
    });
    this.armPhaseTimer(WELCOME_TIMEOUT_MS, 'The backend did not answer the hello.');
  }

  private handleMessage(data: string): void {
    const attempt = this.current;
    if (!attempt) return;
    this.lastFrameAt = this.now();
    let event: unknown;
    try {
      event = JSON.parse(data);
    } catch {
      this.deps.log.warn('ignoring a non-JSON frame');
      return;
    }
    if (!isServerEvent(event)) return;
    if (attempt.phase === 'handshake') {
      if (event.type !== 'welcome') {
        this.deps.log.warn('expected welcome, got', event.type);
        return;
      }
      this.clearPhaseTimer();
      attempt.phase = 'open';
      this.failures = 0;
      if (event.conversation_id) this.deps.conversation?.write(event.conversation_id);
      this.publish({
        state: 'connected',
        sessionId: event.session_id,
        profile: event.profile,
        capabilities: event.capabilities ?? [],
        ...(event.page_budget ? { pageBudget: event.page_budget } : {}),
        ...(event.conversation_id
          ? { conversationId: event.conversation_id, conversationTurns: event.conversation_turns ?? 0 } : {}),
      });
      this.startPing();
      this.finishAttempt();
      return;
    }
    if (event.type === 'pong') return;
    if (event.type === 'config.state' && !event.error) {
      // The options page switched the profile or a provider: capture for the new brain from now on.
      const { updatedAt: _updatedAt, ...current } = this.info;
      this.publish({
        ...current,
        profile: event.profile,
        capabilities: (event.capabilities ?? []).map((entry) => entry.status),
        ...(event.page_budget ? { pageBudget: event.page_budget } : {}),
      });
    }
    this.deps.onServerEvent(event);
  }

  private handleClose(code: number, reason: string): void {
    const attempt = this.current;
    if (!attempt) return;
    this.teardown();
    if (attempt.phase === 'open') this.deps.onSessionEnded?.();
    const outcome = classifyClose(code, attempt.phase);
    this.deps.log.info('socket closed', { code, phase: attempt.phase, outcome });
    if (outcome === 'unauthorized') {
      void this.deps.writeBlocked(attempt.fingerprint);
      this.enterTerminal({
        state: 'unauthorized',
        closeCode: code,
        detail: 'The backend rejected the pairing token. Re-pair to continue.',
      });
      return;
    }
    void this.afterFailure(this.generation, outcome, code, reason, attempt.healthUrl);
  }

  /** Abandons a socket that stopped making progress (connect/welcome timeout, silence) as if it closed abnormally. */
  private abandon(detail: string): void {
    const attempt = this.current;
    if (!attempt) return;
    try {
      attempt.socket.close(CLOSE_NORMAL, 'timeout');
    } catch {
      // ignore
    }
    this.deps.log.warn(detail);
    this.handleClose(CLOSE_ABNORMAL, '');
  }

  private async afterFailure(
    generation: number,
    outcome: Exclude<CloseOutcome, 'unauthorized'>,
    code: number,
    reason: string,
    healthUrl: string,
  ): Promise<void> {
    if (outcome === 'probe') {
      const reachable = await this.deps.probeHealth(healthUrl);
      if (generation !== this.generation) return;
      this.scheduleRetry(
        reachable
          ? {
              state: 'rejected',
              closeCode: code,
              detail:
                'The backend is reachable but rejected the connection. Is this extension ID in allowed_extension_ids?',
            }
          : { state: 'disconnected', closeCode: code, detail: 'The backend is not reachable. Is `clicky serve` running?' },
      );
      return;
    }
    if (outcome === 'rejected') {
      this.scheduleRetry({ state: 'rejected', closeCode: code, detail: reason || 'The backend refused this client.' });
      return;
    }
    this.scheduleRetry({ state: 'disconnected', closeCode: code, detail: describeClose(code, reason) });
  }

  private scheduleRetry(info: Omit<ConnInfo, 'updatedAt'>): void {
    const delay = backoffDelay(this.failures, this.random);
    this.failures += 1;
    this.publish(info);
    this.clearRetry();
    this.retryTimer = this.timers.setTimeout(() => {
      this.retryTimer = null;
      void this.ensureConnected('backoff');
    }, delay);
    this.finishAttempt();
  }

  private enterTerminal(info: Omit<ConnInfo, 'updatedAt'>): void {
    this.terminal = true;
    this.clearRetry();
    this.publish(info);
    this.finishAttempt();
  }

  private finishAttempt(): void {
    const settle = this.settle;
    this.settle = null;
    settle?.();
  }

  private publish(info: Omit<ConnInfo, 'updatedAt'>): void {
    this.info = { ...info, updatedAt: this.now() };
    this.deps.publish(this.info);
  }

  private sendRaw(event: ClientEvent): boolean {
    const attempt = this.current;
    if (!attempt) return false;
    try {
      attempt.socket.send(JSON.stringify(event));
      return true;
    } catch (error) {
      this.deps.log.warn('send failed', error);
      return false;
    }
  }

  private startPing(): void {
    this.stopPing();
    this.pingTimer = this.timers.setInterval(() => {
      if (this.now() - this.lastFrameAt > SILENCE_TIMEOUT_MS) {
        this.abandon('No frame from the backend for too long; reconnecting.');
        return;
      }
      this.sendRaw({ type: 'ping', t: this.now() });
    }, PING_INTERVAL_MS);
  }

  private stopPing(): void {
    if (this.pingTimer !== null) this.timers.clearInterval(this.pingTimer);
    this.pingTimer = null;
  }

  private armPhaseTimer(ms: number, detail: string): void {
    this.clearPhaseTimer();
    this.phaseTimer = this.timers.setTimeout(() => {
      this.phaseTimer = null;
      this.abandon(detail);
    }, ms);
  }

  private clearPhaseTimer(): void {
    if (this.phaseTimer !== null) this.timers.clearTimeout(this.phaseTimer);
    this.phaseTimer = null;
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) this.timers.clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  private teardown(): void {
    this.clearPhaseTimer();
    this.stopPing();
    this.current = null;
  }
}
