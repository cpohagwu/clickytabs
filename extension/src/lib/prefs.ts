/**
 * Non-sensitive preferences. Safe to import from content scripts: no storage access, no backend details.
 */

export const PTT_CHORDS = ['hold-right-ctrl', 'hold-ctrl-space'] as const;
export type PttChord = (typeof PTT_CHORDS)[number];
export const DEFAULT_PTT_CHORD: PttChord = 'hold-right-ctrl';

export const PTT_CHORD_LABELS: Record<PttChord, string> = {
  'hold-right-ctrl': 'Hold Right Ctrl',
  'hold-ctrl-space': 'Hold Ctrl+Space',
};

/** `dock`: the orb rests at the screen edge (draggable). `follow`: it hovers beside the cursor, never blocking clicks. */
export const COMPANION_MODES = ['dock', 'follow'] as const;
export type CompanionMode = (typeof COMPANION_MODES)[number];
export const DEFAULT_COMPANION_MODE: CompanionMode = 'dock';

export const COMPANION_MODE_LABELS: Record<CompanionMode, string> = {
  dock: 'Dock at the screen edge',
  follow: 'Follow the cursor',
};

export function isCompanionMode(value: unknown): value is CompanionMode {
  return typeof value === 'string' && (COMPANION_MODES as readonly string[]).includes(value);
}

/** What a content script may learn about preferences (computed by the service worker for the sender's tab). */
export interface ContentPrefs {
  pttChord: PttChord;
  companionMode: CompanionMode;
  siteDisabled: boolean;
  devTimeline: boolean;
}

export function isPttChord(value: unknown): value is PttChord {
  return typeof value === 'string' && (PTT_CHORDS as readonly string[]).includes(value);
}

const HOSTNAME = /^(?:[a-z0-9-]+\.)*[a-z0-9-]+$|^\[[0-9a-f:.]+\]$/;

/**
 * Normalises user input such as `https://www.Example.com/path`, `*.example.com` or `example.com.` to a lowercase
 * hostname. Returns null when nothing usable remains.
 */
export function normalizeSite(input: string): string | null {
  let text = input.trim().toLowerCase();
  if (!text) return null;
  text = text.replace(/^\*\./, '');
  let host: string;
  try {
    host = new URL(text.includes('://') ? text : `http://${text}`).hostname;
  } catch {
    return null;
  }
  host = host.replace(/\.$/, '');
  return host && HOSTNAME.test(host) ? host : null;
}

/** True when the page's hostname equals a disabled site or is one of its subdomains. */
export function isSiteDisabled(pageUrl: string | undefined, disabledSites: readonly string[]): boolean {
  if (!pageUrl || disabledSites.length === 0) return false;
  let host: string;
  try {
    host = new URL(pageUrl).hostname.toLowerCase().replace(/\.$/, '');
  } catch {
    return false;
  }
  if (!host) return false;
  return disabledSites.some((site) => host === site || host.endsWith(`.${site}`));
}
