/**
 * Backend URL policy.
 *
 * - Only `ws://` and `wss://` are accepted, without credentials or fragments.
 * - The pairing token is never sent over plain `ws://` unless the host is loopback, so a non-loopback `ws://` URL
 *   is rejected outright (there is nothing useful to do on such a connection without the token).
 */

export type BackendUrlCheck =
  | { ok: true; url: string; loopback: boolean; secure: boolean; healthUrl: string }
  | { ok: false; error: string };

const IPV4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;

/** `localhost`, `*.localhost` (Chrome resolves these to loopback itself), 127.0.0.0/8 and `::1`. */
export function isLoopbackHostname(hostname: string): boolean {
  const host = hostname.trim().toLowerCase().replace(/^\[(.*)\]$/, '$1').replace(/\.$/, '');
  if (host === 'localhost' || host.endsWith('.localhost')) return true;
  if (host === '::1') return true;
  const octets = IPV4.exec(host);
  if (!octets) return false;
  const parts = octets.slice(1).map(Number);
  return parts.every((part) => part <= 255) && parts[0] === 127;
}

/** `ws(s)://host:port/v1/ws` → `http(s)://host:port/healthz`. */
export function healthUrlFor(wsUrl: string): string {
  const url = new URL(wsUrl);
  url.protocol = url.protocol === 'wss:' ? 'https:' : 'http:';
  url.pathname = '/healthz';
  url.search = '';
  url.hash = '';
  return url.href;
}

export function checkBackendUrl(raw: string): BackendUrlCheck {
  const text = raw.trim();
  if (!text) return { ok: false, error: 'Enter the backend URL (for example ws://127.0.0.1:8765/v1/ws).' };
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return { ok: false, error: 'That is not a valid URL.' };
  }
  if (url.protocol !== 'ws:' && url.protocol !== 'wss:') {
    return { ok: false, error: 'The backend URL must start with ws:// or wss://.' };
  }
  if (url.username || url.password) {
    return { ok: false, error: 'Do not put credentials in the URL; paste the pairing token instead.' };
  }
  if (url.hash) return { ok: false, error: 'The backend URL must not contain a #fragment.' };
  const loopback = isLoopbackHostname(url.hostname);
  const secure = url.protocol === 'wss:';
  if (!secure && !loopback) {
    return {
      ok: false,
      error: 'Use wss:// for a backend that is not on this computer: the token is never sent over plain ws://.',
    };
  }
  return { ok: true, url: url.href, loopback, secure, healthUrl: healthUrlFor(url.href) };
}

/** Defence in depth for the socket layer: may a hello carrying the token go to this URL? */
export function mayCarryToken(wsUrl: string): boolean {
  const check = checkBackendUrl(wsUrl);
  return check.ok && (check.secure || check.loopback);
}
