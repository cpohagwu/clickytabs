/**
 * The single offscreen document (mic capture + audio playback). Service worker only.
 *
 * Created with USER_MEDIA + AUDIO_PLAYBACK. Chrome may still close it (AUDIO_PLAYBACK documents close after ~30 s of
 * silence), so every use goes through {@link ensureOffscreen}. getUserMedia in an offscreen document cannot show a
 * permission prompt: the grant comes from the onboarding page (same extension origin).
 */
import { browser } from 'wxt/browser';

import { APP_NAME } from './brand';

export const OFFSCREEN_PATH = '/offscreen.html';

let creating: Promise<void> | null = null;

async function exists(url: string): Promise<boolean> {
  const contexts = await browser.runtime.getContexts({
    contextTypes: [browser.runtime.ContextType.OFFSCREEN_DOCUMENT],
    documentUrls: [url],
  });
  return contexts.length > 0;
}

export async function ensureOffscreen(): Promise<void> {
  const url = browser.runtime.getURL(OFFSCREEN_PATH);
  if (await exists(url)) return;
  creating ??= browser.offscreen
    .createDocument({
      url: OFFSCREEN_PATH,
      reasons: [browser.offscreen.Reason.USER_MEDIA, browser.offscreen.Reason.AUDIO_PLAYBACK],
      justification: `Push-to-talk microphone capture and spoken replies for the ${APP_NAME} companion.`,
    })
    .catch(async (error: unknown) => {
      // A concurrent creation from another wake-up wins the race; anything else is a real failure.
      if (!(await exists(url))) throw error;
    })
    .finally(() => {
      creating = null;
    });
  await creating;
}
