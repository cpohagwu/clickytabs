/**
 * Backend connection state, as persisted by the service worker in `chrome.storage.session` under {@link CONN_KEY}.
 * Extension pages may read it directly; content scripts only ever see the {@link ConnState} via `status.get`.
 */
import type { CapabilityStatus, PageBudget } from './protocol.gen';

export const CONN_KEY = 'conn';

export const CONN_STATES = [
  'unpaired', // no token (or an unusable backend URL): needs onboarding
  'connecting',
  'connected',
  'disconnected', // backend unreachable or connection dropped: retrying with backoff + alarm
  'unauthorized', // close 4401: terminal until the token or URL changes
  'rejected', // backend reachable but the handshake failed (extension ID not allowed?): retrying
] as const;
export type ConnState = (typeof CONN_STATES)[number];

export interface ConnInfo {
  state: ConnState;
  sessionId?: string;
  closeCode?: number;
  detail?: string;
  profile?: string;
  capabilities?: CapabilityStatus[];
  /** How much page context the active brain gets (from `welcome`). */
  pageBudget?: PageBudget;
  /** The conversation this session continues, and how many answered turns it held at `welcome`. */
  conversationId?: string;
  conversationTurns?: number;
  updatedAt: number;
}

export const CONN_LABELS: Record<ConnState, string> = {
  unpaired: 'Not paired',
  connecting: 'Connecting…',
  connected: 'Connected',
  disconnected: 'Backend offline',
  unauthorized: 'Token rejected',
  rejected: 'Handshake rejected',
};

/** States in which the user has to (re-)pair before anything can work. */
export function needsPairing(state: ConnState): boolean {
  return state === 'unpaired' || state === 'unauthorized' || state === 'rejected';
}

export function isConnState(value: unknown): value is ConnState {
  return typeof value === 'string' && (CONN_STATES as readonly string[]).includes(value);
}

export function isConnInfo(value: unknown): value is ConnInfo {
  if (typeof value !== 'object' || value === null) return false;
  const info = value as Record<string, unknown>;
  return isConnState(info.state) && typeof info.updatedAt === 'number';
}
