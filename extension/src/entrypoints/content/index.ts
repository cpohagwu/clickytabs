/**
 * The in-page companion (top frame only): a closed shadow root holding the orb, the HUD, the pointer overlay canvas
 * and, on demand, the sandboxed scene iframe.
 *
 * It never talks to the backend (page-origin requests are subject to the page's CSP, CORS and the Local Network
 * Access prompt) and cannot read chrome.storage.local (locked to trusted contexts): everything goes through
 * service-worker messages. tests/unit/guard.test.ts enforces both.
 */
import '@/lib/trusted-types-guard'; // first: runs before Svelte creates its Trusted Types policy
import './style.css';

import { mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { browser } from 'wxt/browser';
import { createShadowRootUi } from 'wxt/utils/content-script-ui/shadow-root';
import { defineContentScript } from 'wxt/utils/define-content-script';

import { companion, displayOrb } from '@/lib/companion';
import { CompanionController } from '@/lib/companion-controller';
import { isConnState } from '@/lib/conn';
import { createLogger } from '@/lib/log';
import type { Ack, DebugStateReply, PublicStatus, SwRequest, TabMessage } from '@/lib/messages';
import { parseTabMessage } from '@/lib/messages';
import type { ContentPrefs as ContentPrefsReply } from '@/lib/prefs';
import { SceneHost } from '@/lib/scene-host';
import { StageController } from '@/lib/scene/stage';
import { TrackerController } from '@/lib/task/tracker';

import App from './App.svelte';

/** Pointer/mouse events that must not reach page listeners (e.g. "click outside closes the menu"). */
const CONTAINED_EVENTS = ['pointerdown', 'pointerup', 'mousedown', 'mouseup', 'click', 'dblclick', 'contextmenu'];

export default defineContentScript({
  matches: ['<all_urls>'],
  runAt: 'document_idle',
  cssInjectionMode: 'ui',

  async main(ctx) {
    const log = createLogger('content');
    const prefs = await ask<ContentPrefsReply>({ type: 'settings.get' });
    // A second copy of this script (the install-time injection racing the manifest one) invalidates this one. WXT
    // signals that with an abort, and onInvalidated callbacks registered after it never run, so a copy invalidated
    // during an await must stop here: otherwise it mounts a second companion that answers messages forever.
    if (ctx.isInvalid || prefs?.siteDisabled) return;
    if (prefs) {
      companion.update((s) => ({ ...s, chord: prefs.pttChord, mode: prefs.companionMode, devTimeline: prefs.devTimeline }));
    }

    const controller = new CompanionController({
      store: companion,
      request: ask,
      reducedMotion: () => matchMedia('(prefers-reduced-motion: reduce)').matches,
      log: (message, error) => log.warn(message, error),
    });

    const sceneUrl = (): string => browser.runtime.getURL('/scene.html');
    const stage = new StageController({
      sceneUrl,
      sendResult: (result) => void ask<Ack>({ type: 'scene.result', ...result }),
    });
    const tracker = new TrackerController({
      stop: (taskId) => void ask<Ack>({ type: 'task.stop', taskId }),
      answer: (taskId, requestId, approved) => void ask<Ack>({ type: 'task.answer', taskId, requestId, approved }),
      open: (taskId, url) => void ask<Ack>({ type: 'task.open', taskId, url }),
    });
    let sceneHost: SceneHost | null = null;
    const ui = await createShadowRootUi(ctx, {
      name: 'clickytabs-ui',
      position: 'inline',
      anchor: 'body',
      append: 'last',
      mode: 'closed',
      isolateEvents: ['keydown', 'keyup', 'keypress'],
      onMount: (container, shadow) => {
        for (const type of CONTAINED_EVENTS) shadow.addEventListener(type, (event) => event.stopPropagation());
        return mount(App, {
          target: container,
          props: {
            controller,
            stage,
            tracker,
            onSceneSlot: (slot: HTMLElement) => {
              sceneHost = new SceneHost(slot, sceneUrl);
            },
          },
        });
      },
      onRemove: (app) => {
        controller.destroy();
        stage.destroy();
        sceneHost?.destroy();
        if (app) void unmount(app);
      },
    });
    if (ctx.isInvalid) return; // replaced while the UI's styles loaded (see above); from here on it is synchronous
    ui.mount();

    const applyStatus = (status: PublicStatus | undefined): void => {
      if (!status || !isConnState(status.state)) return;
      if (status.state !== 'connected') stage.connectionLost();
      companion.update((s) => ({ ...s, conn: status.state, orb: status.orb }));
    };
    const refreshStatus = async (): Promise<void> => applyStatus(await ask<PublicStatus>({ type: 'status.get' }));
    void refreshStatus();
    ctx.addEventListener(document, 'visibilitychange', () => {
      if (document.visibilityState === 'visible') void refreshStatus();
    });

    const onMessage = (raw: unknown, _sender: unknown, sendResponse: (reply: unknown) => void): true | undefined => {
      const message: TabMessage | null = parseTabMessage(raw);
      if (!message) return undefined;
      switch (message.type) {
        case 'status.changed':
          applyStatus(message.status);
          return undefined;
        case 'worker.started':
          controller.workerRestarted();
          tracker.workerRestarted();
          stage.connectionLost();
          return undefined;
        case 'orb.state':
          companion.update((s) => ({ ...s, orb: message.state }));
          return undefined;
        case 'ptt.state':
          companion.update((s) => ({ ...s, holding: message.holding }));
          if (!message.holding) controller.onPttReleased();
          return undefined;
        case 'voice.turn':
          companion.update((s) => ({ ...s, visible: true }));
          controller.beginVoiceTurn(message.turnId);
          return undefined;
        case 'voice.error':
          controller.voiceError(message.message);
          return undefined;
        case 'voice.level':
          controller.onLevel(message.source, message.level);
          return undefined;
        case 'speech.playback':
          controller.onPlayback(message.turnId, message.seq, message.state, message.inMs);
          return undefined;
        case 'orb.toggle':
          companion.update((s) => ({ ...s, visible: !s.visible }));
          return undefined;
        case 'prefs.changed':
          void ask<ContentPrefsReply>({ type: 'settings.get' }).then((next) => {
            if (!next) return;
            if (next.siteDisabled) ui.remove();
            else companion.update((s) => ({ ...s, chord: next.pttChord, mode: next.companionMode, devTimeline: next.devTimeline }));
          });
          return undefined;
        case 'turn.event':
          controller.onTurnEvent(message.event);
          return undefined;
        case 'demo.run':
          companion.update((s) => ({ ...s, visible: true }));
          void controller.runDemo();
          sendResponse({ ok: true } satisfies Ack);
          return undefined;
        case 'context.snapshot': {
          const snapshot = { ...controller.snapshotForTurn(message.budget, message.hideUi), scene: stage.openScene() };
          // Let the hidden companion reach the screen before the worker captures it.
          void (message.hideUi ? afterPaint() : Promise.resolve()).then(() => sendResponse(snapshot));
          return true;
        }
        case 'context.restore':
          controller.restoreAfterCapture(message.image);
          return undefined;
        case 'debug.marks':
          companion.update((s) => ({ ...s, visible: true }));
          controller.showMarks(message.budget);
          sendResponse({ ok: true } satisfies Ack);
          return undefined;
        case 'debug.state':
          void debugState(ui.mounted != null, controller, stage, tracker).then(sendResponse);
          return true;
        case 'scene.event':
          companion.update((s) => ({ ...s, visible: true }));
          stage.onEvent(message.event);
          sendResponse({ ok: true } satisfies Ack);          // the worker answers "unavailable" when no tab replies
          return undefined;
        case 'task.event':
          companion.update((s) => ({ ...s, visible: true }));
          tracker.onEvent(message.event);
          sendResponse({ ok: true } satisfies Ack);
          return undefined;
        case 'context.storage':
          sendResponse(exportLocalStorage());
          return undefined;
        case 'debug.taskAnswer':
          tracker.answer(message.approved);
          sendResponse({ ok: true } satisfies Ack);
          return undefined;
        case 'debug.sceneClose':
          stage.close();
          sendResponse({ ok: true } satisfies Ack);
          return undefined;
        case 'debug.sceneRender':
          companion.update((s) => ({ ...s, visible: true }));
          void stage.renderDirect(message.kind, message.code, message.title).then(sendResponse);
          return true;
        case 'debug.scenePing':
          if (!sceneHost) {
            sendResponse({ ok: false, error: 'the companion is not mounted' });
            return undefined;
          }
          void sceneHost.ping().then(sendResponse);
          return true;
      }
    };
    browser.runtime.onMessage.addListener(onMessage);
    ctx.onInvalidated(() => browser.runtime.onMessage.removeListener(onMessage));
  },
});

/**
 * Resolves once the hidden companion is on screen: two animation frames, then a short settle, because the surface
 * captureVisibleTab copies can trail the renderer's own frames (measured: two frames alone sometimes returned the
 * previous picture). Falls back to a timer when frames are throttled.
 */
const CAPTURE_SETTLE_MS = 60;
function afterPaint(): Promise<void> {
  return new Promise((resolve) => {
    let done = false;
    const finish = (): void => {
      if (done) return;
      done = true;
      setTimeout(resolve, CAPTURE_SETTLE_MS);
    };
    const timer = setTimeout(finish, 150);
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        clearTimeout(timer);
        finish();
      }));
  });
}

async function ask<T>(request: SwRequest): Promise<T | undefined> {
  try {
    return (await browser.runtime.sendMessage(request)) as T;
  } catch {
    return undefined; // worker restarting or extension reloaded: the next event retries
  }
}

/**
 * The page origin's localStorage for a browser task the user allowed to use their login (the worker checks the
 * origin is on the task's site). Sandboxed or opaque-origin pages have none.
 */
function exportLocalStorage(): { origin: string; entries: Record<string, string> } | null {
  try {
    const entries: Record<string, string> = {};
    for (let i = 0; i < localStorage.length && i < 500; i += 1) {
      const key = localStorage.key(i);
      if (key !== null) entries[key] = localStorage.getItem(key) ?? '';
    }
    return { origin: location.origin, entries };
  } catch {
    return null;
  }
}

async function debugState(mounted: boolean, controller: CompanionController,
  stage: StageController, tracker: TrackerController): Promise<DebugStateReply> {
  const state = get(companion);
  return {
    mounted,
    visible: state.visible,
    orbState: displayOrb(state),
    conn: state.conn,
    storageLocked: await isStorageLocked(),
    companionMode: state.mode,
    hudOpen: state.hudOpen,
    annotations: controller.annotations,
    animating: controller.animating,
    lastDirective: controller.lastDirective,
    caption: state.caption,
    timeline: state.timeline,
    devTimeline: state.devTimeline,
    error: state.error,
    busy: state.busy,
    layout: controller.layout(),
    scene: stage.debug(),
    task: tracker.debug(),
  };
}

/** True when this content script cannot read chrome.storage.local (the worker's setAccessLevel took effect). */
async function isStorageLocked(): Promise<boolean> {
  try {
    if (!browser.storage?.local) return true;
    await browser.storage.local.get('token');
    return false;
  } catch {
    return true;
  }
}
