/**
 * Sites, cookies and consent grants for browser tasks. Pure (no extension APIs), so it is unit-tested.
 *
 * A "site" is a registrable domain from the Public Suffix List (tldts, private domains included: two github.io
 * pages are different sites). The cookie scope of a task is always the site of the user's real tab, worked out here
 * from the tab URL the service worker reads from Chrome - never from anything the model wrote. A task whose start URL
 * is on another site is cross-site: it always asks, and can never be granted "Always".
 */
import { getDomain } from 'tldts';

import type { CookieRecord } from '../protocol.gen';

export function siteOf(url: string | null | undefined): string | null {
  if (!url) return null;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
  const site = getDomain(parsed.hostname, { allowPrivateDomains: true });
  return site ? site.toLowerCase() : null;
}

export function hostInSite(host: string, site: string): boolean {
  const bare = host.replace(/^\./, '').toLowerCase();
  return bare === site || bare.endsWith(`.${site}`);
}

export interface TaskSites {
  /** The site of the user's tab: the only site whose cookies may be shared. */
  tabSite: string;
  /** Where the task acts (the start URL's site, or the tab's). */
  taskSite: string;
  crossSite: boolean;
  /** What the backend may navigate to. */
  sites: string[];
}

/** null when the tab (or the start URL) is not on a website a task can use. */
export function taskSites(tabUrl: string | null | undefined, startUrl: string | null | undefined): TaskSites | null {
  const tabSite = siteOf(tabUrl);
  if (!tabSite) return null;
  const taskSite = startUrl ? siteOf(startUrl) : tabSite;
  if (!taskSite) return null;
  const crossSite = taskSite !== tabSite;
  return { tabSite, taskSite, crossSite, sites: crossSite ? [tabSite, taskSite] : [tabSite] };
}

// ------------------------------------------------------------------------------------------------------ cookies

/** The fields of chrome.cookies.Cookie this module reads. */
export interface ChromeCookie {
  name: string;
  value: string;
  domain: string;
  path: string;
  expirationDate?: number;
  httpOnly: boolean;
  secure: boolean;
  sameSite: 'no_restriction' | 'lax' | 'strict' | 'unspecified';
  hostOnly: boolean;
  session: boolean;
  partitionKey?: { topLevelSite?: string };
}

const SAME_SITE: Record<ChromeCookie['sameSite'], CookieRecord['same_site']> = {
  no_restriction: 'None', lax: 'Lax', strict: 'Strict', unspecified: null,
};

/** Chrome cookies -> the wire form, keeping only the site's own (and dropping duplicates). */
export function toCookieRecords(cookies: ChromeCookie[], site: string): CookieRecord[] {
  const seen = new Set<string>();
  const records: CookieRecord[] = [];
  for (const cookie of cookies) {
    if (!hostInSite(cookie.domain, site)) continue;
    const key = `${cookie.name}|${cookie.domain}|${cookie.path}|${cookie.partitionKey?.topLevelSite ?? ''}`;
    if (seen.has(key)) continue;
    seen.add(key);
    records.push({
      name: cookie.name, value: cookie.value, domain: cookie.domain, path: cookie.path || '/',
      expires: cookie.session || cookie.expirationDate === undefined ? null : cookie.expirationDate,
      http_only: cookie.httpOnly, secure: cookie.secure, same_site: SAME_SITE[cookie.sameSite] ?? null,
      host_only: cookie.hostOnly, partition_key: cookie.partitionKey?.topLevelSite ?? null,
    });
  }
  return records.slice(0, 500);
}

// ------------------------------------------------------------------------------------------------------ grants

export const GRANTS_KEY = 'taskGrants';

/** "Always allow" choices: site -> whether the login may be shared. Same-site tasks only. */
/** `at`: when the user chose "Always"; `used`: the last task it let start without asking. */
export type Grants = Record<string, { session: boolean; at: number; used?: number }>;

/** A grant lapses 90 days after it was last used (or given, if never used). */
export const GRANT_TTL_MS = 90 * 24 * 60 * 60 * 1000;
/** At most this many sites; the least recently used go first. */
export const MAX_GRANTS = 50;

export function parseGrants(raw: unknown, now = Date.now()): Grants {
  const entries: [string, Grants[string]][] = [];
  if (typeof raw !== 'object' || raw === null) return {};
  for (const [site, value] of Object.entries(raw as Record<string, unknown>)) {
    if (!siteOf(`https://${site}/`) || siteOf(`https://${site}/`) !== site) continue;
    if (typeof value !== 'object' || value === null) continue;
    const entry = value as { session?: unknown; at?: unknown; used?: unknown };
    if (typeof entry.session !== 'boolean' || typeof entry.at !== 'number' || !Number.isFinite(entry.at)) continue;
    const used = typeof entry.used === 'number' && Number.isFinite(entry.used) ? entry.used : undefined;
    const last = Math.max(entry.at, used ?? 0);
    if (last > now + 60_000 || now - last > GRANT_TTL_MS) continue;    // expired, or dated in the future
    entries.push([site, used === undefined ? { session: entry.session, at: entry.at }
      : { session: entry.session, at: entry.at, used }]);
  }
  entries.sort(([, a], [, b]) => Math.max(b.at, b.used ?? 0) - Math.max(a.at, a.used ?? 0));
  return Object.fromEntries(entries.slice(0, MAX_GRANTS));
}

/** May this task skip the consent window? Only a same-site task the user chose "Always" for, with no more access. */
export function grantCovers(grants: Grants, sites: TaskSites, wantsSession: boolean): boolean {
  if (sites.crossSite) return false;
  const grant = grants[sites.tabSite];
  return grant !== undefined && (grant.session || !wantsSession);
}
