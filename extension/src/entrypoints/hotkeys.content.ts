/**
 * Push-to-talk relay, injected into EVERY frame (including about:blank / srcdoc frames, where rich editors such as
 * Google Docs take keystrokes). It only reports raw chord events; the service worker owns the hold (lib/ptt.ts)
 * and ends it if a keyup is lost. No sockets, no storage, no page data.
 */
import { browser } from 'wxt/browser';
import { defineContentScript } from 'wxt/utils/define-content-script';

import { ChordDetector, isInCodeEditor } from '@/lib/chord';
import type { FrameReleaseReason, SwResponseMap } from '@/lib/messages';
import { parseTabMessage } from '@/lib/messages';

export default defineContentScript({
  matches: ['<all_urls>'],
  allFrames: true,
  matchAboutBlank: true,
  matchOriginAsFallback: true,
  runAt: 'document_start',

  async main(ctx) {
    const send = (message: { type: 'ptt.down' } | { type: 'ptt.up'; reason: FrameReleaseReason }): void => {
      browser.runtime.sendMessage(message).catch(() => undefined);
    };
    const detector = new ChordDetector('hold-right-ctrl', {
      down: () => send({ type: 'ptt.down' }),
      up: (reason) => send({ type: 'ptt.up', reason }),
    });
    let enabled = false;

    const loadPrefs = async (): Promise<void> => {
      try {
        const prefs = (await browser.runtime.sendMessage({ type: 'settings.get' })) as
          | SwResponseMap['settings.get']
          | undefined;
        if (!prefs) return;
        enabled = !prefs.siteDisabled;
        detector.setChord(prefs.pttChord);
        if (!enabled) detector.release('cancel');
      } catch {
        // worker restarting: keep the previous preferences
      }
    };
    await loadPrefs();
    if (ctx.isInvalid) return; // a newer copy started meanwhile (see content/index.ts): it owns the frame now

    ctx.addEventListener(window, 'keydown', (event: KeyboardEvent) => {
      if (!enabled) return;
      if (detector.keydown(event, isInCodeEditor(document.activeElement)) === 'prevent') event.preventDefault();
    }, { capture: true });
    ctx.addEventListener(window, 'keyup', (event: KeyboardEvent) => {
      if (!enabled) return;
      if (detector.keyup(event) === 'prevent') event.preventDefault();
    }, { capture: true });
    ctx.addEventListener(window, 'mousedown', () => detector.mousedown(), { capture: true });
    ctx.addEventListener(window, 'blur', () => detector.release('blur'));
    ctx.addEventListener(window, 'pagehide', () => detector.release('pagehide'));
    ctx.addEventListener(document, 'visibilitychange', () => {
      if (document.visibilityState === 'hidden') detector.release('hidden');
    });

    const onMessage = (raw: unknown): undefined => {
      if (parseTabMessage(raw)?.type === 'prefs.changed') void loadPrefs();
      return undefined;
    };
    browser.runtime.onMessage.addListener(onMessage);
    ctx.onInvalidated(() => {
      detector.release('cancel');
      browser.runtime.onMessage.removeListener(onMessage);
    });
  },
});
