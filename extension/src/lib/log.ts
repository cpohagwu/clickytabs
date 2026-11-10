/**
 * Namespaced console logging for every extension context.
 *
 * Callers must never pass secrets, and the logger masks anything that looks like one anyway: object fields named
 * like credentials, `token=` query parameters and `Bearer` values are replaced before reaching the console.
 */

export interface Logger {
  debug(...args: unknown[]): void;
  info(...args: unknown[]): void;
  warn(...args: unknown[]): void;
  error(...args: unknown[]): void;
}

const MASK = '[redacted]';
const SECRET_KEY = /^(token|auth|authorization|password|passwd|secret|api[-_]?key|access[-_]?token)$/i;
const SECRET_IN_TEXT = /\b(token|api[-_]?key|access[-_]?token)=([^&\s"']+)/gi;
const BEARER = /\bBearer\s+[A-Za-z0-9._~+/=-]+/g;
const MAX_DEPTH = 4;

function redactText(text: string): string {
  return text.replace(SECRET_IN_TEXT, (_m, key: string) => `${key}=${MASK}`).replace(BEARER, `Bearer ${MASK}`);
}

/** Deep-copies `value` with secret-looking fields masked. Errors keep their name/message (redacted). */
export function redact(value: unknown, depth = 0): unknown {
  if (typeof value === 'string') return redactText(value);
  if (value === null || typeof value !== 'object') return value;
  if (value instanceof Error) {
    const copy = new Error(redactText(value.message));
    copy.name = value.name;
    return copy;
  }
  if (depth >= MAX_DEPTH) return '[…]';
  if (Array.isArray(value)) return value.map((item) => redact(item, depth + 1));
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    out[key] = SECRET_KEY.test(key) ? MASK : redact(item, depth + 1);
  }
  return out;
}

export function createLogger(scope: string): Logger {
  const prefix = `[cc:${scope}]`;
  const emit =
    (method: 'debug' | 'info' | 'warn' | 'error') =>
    (...args: unknown[]): void => {
      console[method](prefix, ...args.map((arg) => redact(arg)));
    };
  return { debug: emit('debug'), info: emit('info'), warn: emit('warn'), error: emit('error') };
}
