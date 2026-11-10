/** Helpers for extension pages (popup, onboarding, options) talking to the service worker. Not for content scripts. */
import { browser } from 'wxt/browser';

import { CONN_KEY, type ConnInfo, isConnInfo } from './conn';
import type { MicInfo, SwRequest, SwResponseMap } from './messages';
import { MIC_KEY } from './messages';

export async function request<R extends SwRequest>(message: R): Promise<SwResponseMap[R['type']]> {
  return (await browser.runtime.sendMessage(message)) as SwResponseMap[R['type']];
}

export async function readConn(): Promise<ConnInfo | null> {
  const stored = await browser.storage.session.get(CONN_KEY);
  const value: unknown = stored[CONN_KEY];
  return isConnInfo(value) ? value : null;
}

/** Calls `onChange` with the current connection state and on every change. Returns an unsubscribe function. */
export function watchConn(onChange: (info: ConnInfo | null) => void): () => void {
  const listener = (changes: Record<string, { newValue?: unknown }>, area: string): void => {
    if (area === 'session' && CONN_KEY in changes) {
      const value = changes[CONN_KEY]?.newValue;
      onChange(isConnInfo(value) ? value : null);
    }
  };
  browser.storage.onChanged.addListener(listener);
  void readConn().then(onChange);
  return () => browser.storage.onChanged.removeListener(listener);
}

export async function readMic(): Promise<MicInfo | null> {
  const stored = await browser.storage.session.get(MIC_KEY);
  return (stored[MIC_KEY] as MicInfo | undefined) ?? null;
}

export async function openOnboarding(hash = ''): Promise<void> {
  await browser.tabs.create({ url: browser.runtime.getURL(`/onboarding.html${hash}`) });
}
