/**
 * Must be the first import of the content script (before anything that loads Svelte).
 *
 * Svelte's DOM runtime creates a Trusted Types policy named 'svelte-trusted-html' as soon as it loads. On pages whose
 * CSP allow-lists policy names (`trusted-types …`, as GitHub does), that call throws and the whole content script
 * dies before the companion mounts. The name check uses the page's CSP even inside our isolated world, but the page's
 * `require-trusted-types-for` sink enforcement does not apply to extension worlds. So: probe once, and if policies
 * are forbidden here, hide the factory from this isolated world only (the page's own `trustedTypes` is untouched).
 * Svelte then assigns its static, compile-time templates as plain strings.
 */
function policiesForbidden(): boolean {
  const factory = (globalThis as { trustedTypes?: { createPolicy(name: string, rules: object): unknown } }).trustedTypes;
  if (!factory) return false;
  try {
    factory.createPolicy(`clickytabs-probe-${Math.random().toString(36).slice(2)}`, {});
    return false;
  } catch {
    return true;
  }
}

if (policiesForbidden()) {
  Object.defineProperty(globalThis, 'trustedTypes', { value: undefined, configurable: true, writable: true });
}

export {};
