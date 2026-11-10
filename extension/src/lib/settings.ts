/**
 * User settings in `chrome.storage.local` (locked to trusted contexts by the service worker at startup).
 *
 * Only the service worker and extension pages import this module. Content scripts receive {@link ContentPrefs}
 * through `settings.get` and never see the backend URL or the token.
 */
import { browser } from 'wxt/browser';
import type { PagePrefs, SettingsPatch } from './messages';
import {
  type CompanionMode,
  DEFAULT_COMPANION_MODE,
  DEFAULT_PTT_CHORD,
  isCompanionMode,
  isPttChord,
  normalizeSite,
  type PttChord,
} from './prefs';
import { checkBackendUrl } from './url-policy';

export const DEFAULT_BACKEND_URL = 'ws://127.0.0.1:8765/v1/ws';

export interface Settings {
  backendUrl: string;
  token: string;
  pttChord: PttChord;
  companionMode: CompanionMode;
  disabledSites: string[];
  /** Developers: show where the time of the last answer went, under it in the panel. */
  devTimeline: boolean;
}

export const DEFAULT_SETTINGS: Readonly<Settings> = Object.freeze({
  backendUrl: DEFAULT_BACKEND_URL,
  token: '',
  pttChord: DEFAULT_PTT_CHORD,
  companionMode: DEFAULT_COMPANION_MODE,
  disabledSites: [],
  devTimeline: false,
});

export const SETTINGS_KEYS = ['backendUrl', 'token', 'pttChord', 'companionMode', 'disabledSites', 'devTimeline'] as const satisfies readonly (keyof Settings)[];
/** A change to any of these needs a fresh connection (and clears the terminal `unauthorized` state). */
export const CONNECTION_KEYS = ['backendUrl', 'token'] as const satisfies readonly (keyof Settings)[];
/** A change to any of these is pushed to content scripts as `prefs.changed`. */
export const PREF_KEYS = ['pttChord', 'companionMode', 'disabledSites', 'devTimeline'] as const satisfies readonly (keyof Settings)[];

export const MAX_TOKEN_LENGTH = 512;
export const MAX_DISABLED_SITES = 500;
const TOKEN_CHARS = /^[\x21-\x7e]*$/; // printable ASCII, no whitespace

function uniqueSites(values: readonly unknown[]): string[] {
  const sites = new Set<string>();
  for (const value of values) {
    const site = typeof value === 'string' ? normalizeSite(value) : null;
    if (site) sites.add(site);
  }
  return [...sites].slice(0, MAX_DISABLED_SITES);
}

/** Coerces whatever is in storage into well-typed settings (unknown or malformed values fall back to defaults). */
export function normalizeSettings(raw: Record<string, unknown>): Settings {
  return {
    backendUrl:
      typeof raw.backendUrl === 'string' && raw.backendUrl.trim() ? raw.backendUrl.trim() : DEFAULT_BACKEND_URL,
    token: typeof raw.token === 'string' ? raw.token.trim() : '',
    pttChord: isPttChord(raw.pttChord) ? raw.pttChord : DEFAULT_PTT_CHORD,
    companionMode: isCompanionMode(raw.companionMode) ? raw.companionMode : DEFAULT_COMPANION_MODE,
    disabledSites: Array.isArray(raw.disabledSites) ? uniqueSites(raw.disabledSites) : [],
    devTimeline: raw.devTimeline === true,
  };
}

export type PatchCheck = { ok: true; values: Partial<Settings> } | { ok: false; error: string };

/** Semantic validation of a structurally valid patch (see `parseSettingsPatch`). */
export function validateSettingsPatch(patch: SettingsPatch): PatchCheck {
  const values: Partial<Settings> = {};
  if (patch.backendUrl !== undefined) {
    const check = checkBackendUrl(patch.backendUrl);
    if (!check.ok) return { ok: false, error: check.error };
    values.backendUrl = check.url;
  }
  if (patch.token !== undefined) {
    const token = patch.token.trim();
    if (token.length > MAX_TOKEN_LENGTH || !TOKEN_CHARS.test(token)) {
      return { ok: false, error: 'That does not look like a pairing token (copy it from `clicky token show`).' };
    }
    values.token = token;
  }
  if (patch.pttChord !== undefined) values.pttChord = patch.pttChord;
  if (patch.companionMode !== undefined) values.companionMode = patch.companionMode;
  if (patch.devTimeline !== undefined) values.devTimeline = patch.devTimeline;
  if (patch.disabledSites !== undefined) {
    if (patch.disabledSites.length > MAX_DISABLED_SITES) {
      return { ok: false, error: `At most ${MAX_DISABLED_SITES} sites can be disabled.` };
    }
    const invalid = patch.disabledSites.find((site) => normalizeSite(site) === null);
    if (invalid !== undefined) return { ok: false, error: `"${invalid}" is not a valid site.` };
    values.disabledSites = uniqueSites(patch.disabledSites);
  }
  return { ok: true, values };
}

export function toPagePrefs(settings: Settings): PagePrefs {
  return {
    backendUrl: settings.backendUrl,
    hasToken: settings.token.length > 0,
    pttChord: settings.pttChord,
    companionMode: settings.companionMode,
    disabledSites: [...settings.disabledSites],
    devTimeline: settings.devTimeline,
  };
}

export async function loadSettings(): Promise<Settings> {
  return normalizeSettings(await browser.storage.local.get([...SETTINGS_KEYS]));
}

export async function saveSettings(values: Partial<Settings>): Promise<Settings> {
  if (Object.keys(values).length > 0) await browser.storage.local.set(values);
  return loadSettings();
}
