/** In-page companion state (content script only), shared by the entrypoint, the controller and the components. */
import { writable } from 'svelte/store';

import type { ConnState } from './conn';
import type { OrbState } from './messages';
import type { TurnTimeline } from './timeline';
import { type CompanionMode, DEFAULT_COMPANION_MODE, DEFAULT_PTT_CHORD, type PttChord } from './prefs';

export interface CompanionState {
  conn: ConnState | 'unknown';
  /** Orb state owned by the service worker (push-to-talk: `listening`). */
  orb: OrbState;
  /** Orb state owned by this page's controller (HUD turns and the demo). */
  activity: OrbState;
  visible: boolean;
  holding: boolean;
  chord: PttChord;
  mode: CompanionMode;
  hudOpen: boolean;
  /** What the user typed or said last, and the assistant's reply so far. */
  userText: string;
  caption: string;
  turnId: string | null;
  /** A turn or the demo is running (the HUD shows Stop instead of Send). */
  busy: boolean;
  /** 0..1 voice envelope that drives the orb's audio-reactive glow (synthesised when no real audio plays). */
  level: number;
  error: string | null;
  /** The last turn's latency timeline; shown under the answer when `devTimeline` is on. */
  timeline: TurnTimeline | null;
  devTimeline: boolean;
}

export const INITIAL_COMPANION: CompanionState = {
  conn: 'unknown',
  orb: 'idle',
  activity: 'idle',
  visible: true,
  holding: false,
  chord: DEFAULT_PTT_CHORD,
  mode: DEFAULT_COMPANION_MODE,
  hudOpen: false,
  userText: '',
  caption: '',
  turnId: null,
  busy: false,
  level: 0,
  error: null,
  timeline: null,
  devTimeline: false,
};

export const companion = writable<CompanionState>({ ...INITIAL_COMPANION });

/** The worker's state wins while it is active (listening beats a local "speaking"); otherwise the page's own. */
export function displayOrb(state: Pick<CompanionState, 'orb' | 'activity'>): OrbState {
  return state.orb !== 'idle' ? state.orb : state.activity;
}

export const ORB_STATE_LABELS: Record<OrbState, string> = {
  idle: 'Ready',
  listening: 'Listening…',
  thinking: 'Thinking…',
  speaking: 'Speaking',
  acting: 'Working in the browser…',
  error: 'Something went wrong',
};
