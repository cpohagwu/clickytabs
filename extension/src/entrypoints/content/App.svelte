<script lang="ts">
  import { onMount } from 'svelte';
  import { Spring } from 'svelte/motion';

  import AgentTracker from '@/components/AgentTracker.svelte';
  import Hud from '@/components/Hud.svelte';
  import Orb from '@/components/Orb.svelte';
  import ScenePanel from '@/components/ScenePanel.svelte';
  import { APP_NAME } from '@/lib/brand';
  import { companion, displayOrb } from '@/lib/companion';
  import type { CompanionController } from '@/lib/companion-controller';
  import type { LayoutProbe } from '@/lib/messages';
  import { clamp, type Point, type Size } from '@/lib/overlay/geometry';
  import { OverlayRenderer } from '@/lib/overlay/renderer';
  import type { Rgb } from '@/lib/page-context/capture';
  import { PTT_CHORD_LABELS } from '@/lib/prefs';
  import { type StageController, stageStore } from '@/lib/scene/stage';
  import { type TrackerController, trackerStore } from '@/lib/task/tracker';

  interface Props {
    controller: CompanionController;
    /** The scene stage: its state drives the ScenePanel, which renders scenes through it. */
    stage: StageController;
    /** The browser-task tracker. */
    tracker: TrackerController;
    /** Receives the hidden element the sandbox probe frame (debug.scenePing) is mounted in. */
    onSceneSlot: (slot: HTMLElement) => void;
  }

  let { controller, stage, tracker, onSceneSlot }: Props = $props();

  type Side = 'left' | 'right';
  interface Dock {
    side: Side;
    /** Top of the orb in viewport px; null = resting in the bottom corner. */
    y: number | null;
  }

  const ORB_SIZE = 56;
  const MARGIN = 24;
  const FOLLOW_OFFSET = 22;
  const GAP = 14;
  const DRAG_THRESHOLD = 5;
  const BUBBLE_LINGER_MS = 6_000;
  const BUBBLE_CHARS = 180;

  let layer: HTMLDivElement;
  let canvas: HTMLCanvasElement;
  let sceneSlot: HTMLDivElement;
  let orbButton: HTMLButtonElement;
  let sentinelEl: HTMLDivElement;

  const motionQuery = matchMedia('(prefers-reduced-motion: reduce)');
  let reduceMotion = $state(motionQuery.matches);
  let viewport = $state<Size>({ width: innerWidth, height: innerHeight });
  let dock = $state<Dock>({ side: 'right', y: null });
  let dragging = $state(false);
  let capturing = $state(false);
  let lingering = $state(false);
  let drag: { pointerId: number; dx: number; dy: number; startX: number; startY: number } | null = null;
  let suppressClick = false;

  function dockPoint(at: Dock, size: Size): Point {
    const x = at.side === 'right' ? size.width - MARGIN - ORB_SIZE : MARGIN;
    const maxY = Math.max(MARGIN, size.height - MARGIN - ORB_SIZE);
    return { x, y: at.y === null ? maxY : clamp(at.y, MARGIN, maxY) };
  }

  const pos = new Spring<Point>(dockPoint({ side: 'right', y: null }, { width: innerWidth, height: innerHeight }), {
    stiffness: 0.12,
    damping: 0.62,
  });

  const orbState = $derived(displayOrb($companion));
  const following = $derived($companion.mode === 'follow');
  const orbSide = $derived<Side>(pos.current.x + ORB_SIZE / 2 > viewport.width / 2 ? 'right' : 'left');
  const hint = $derived(
    $companion.conn === 'connected'
      ? `${PTT_CHORD_LABELS[$companion.chord]} to talk · click me to type`
      : $companion.conn === 'unpaired' || $companion.conn === 'unauthorized' || $companion.conn === 'rejected'
        ? 'Open the extension popup to pair'
        : 'Waiting for the backend',
  );
  const bubbleText = $derived.by(() => {
    const text = $companion.error ?? $companion.caption;
    if (text.length <= BUBBLE_CHARS) return text;
    const tail = text.slice(-BUBBLE_CHARS);
    return `…${tail.slice(tail.indexOf(' ') + 1)}`;
  });
  const showBubble = $derived(!$companion.hudOpen && bubbleText.length > 0 && ($companion.busy || lingering));

  interface Placement {
    left: string | null;
    right: string | null;
    top: string | null;
    bottom: string | null;
  }

  /**
   * Placement for a panel beside the orb, opening toward the middle of the screen. Applied through style:
   * directives (CSSOM), never a style="" string, which a strict page CSP (style-src without 'unsafe-inline') blocks.
   */
  function besideOrb(at: Point, size: Size): Placement {
    const onRight = at.x + ORB_SIZE / 2 > size.width / 2;
    const upper = at.y + ORB_SIZE / 2 < size.height / 2;
    return {
      left: onRight ? null : `${Math.max(8, at.x + ORB_SIZE + GAP)}px`,
      right: onRight ? `${Math.max(8, size.width - at.x + GAP)}px` : null,
      top: upper ? `${Math.max(8, at.y)}px` : null,
      bottom: upper ? null : `${Math.max(8, size.height - at.y - ORB_SIZE)}px`,
    };
  }

  const CORNER: Placement = { left: null, right: `${MARGIN}px`, top: null, bottom: `${MARGIN}px` };
  const HUD_WIDTH = 360;

  /** The scene stage keeps clear of the orb + HUD column (always reserved, so the stage never jumps). */
  const stageReserve = $derived.by((): { side: Side; width: number } => {
    const hud = Math.min(HUD_WIDTH, viewport.width - 32) + GAP;
    return following ? { side: 'right', width: MARGIN + hud } : { side: orbSide, width: MARGIN + ORB_SIZE + GAP + hud };
  });
  const hudPlacement = $derived(following ? CORNER : besideOrb(pos.current, viewport));
  const bubblePlacement = $derived(besideOrb(pos.current, viewport));

  // Dock mode: rest at the dock point (and spring back to it after a drag, a resize or leaving follow mode).
  $effect(() => {
    if (following || dragging) return;
    void pos.set(dockPoint(dock, viewport), { instant: reduceMotion });
  });

  // Captions stay up for a moment after the reply ends.
  $effect(() => {
    if ($companion.busy) {
      lingering = true;
      return;
    }
    const timer = setTimeout(() => (lingering = false), BUBBLE_LINGER_MS);
    return () => clearTimeout(timer);
  });

  $effect(() => {
    if ($companion.visible) promote();
    else if (layer?.matches(':popover-open')) layer.hidePopover();
  });

  /** Keep the companion in the top layer, above page dialogs/popovers opened after it (best effort). */
  function promote(): void {
    if (!layer?.isConnected || !$companion.visible) return;
    const root = layer.getRootNode() as Document | ShadowRoot;
    const active = root.activeElement;
    const focused = active instanceof HTMLElement && layer.contains(active) ? active : null;
    try {
      if (layer.matches(':popover-open')) layer.hidePopover();
      layer.showPopover();
    } catch {
      // popover unsupported or not allowed right now: the z-index fallback still applies
    }
    focused?.focus({ preventScroll: true }); // re-showing would otherwise drop the caret out of the HUD input
  }

  /** Synchronous (no Svelte tick): the worker captures right after the snapshot reply. Opacity keeps focus. */
  function setCapturing(on: boolean, sentinel?: Rgb): void {
    if (sentinel) sentinelEl.style.background = `rgb(${sentinel.join(' ')})`;
    if (on) layer.dataset.capturing = 'true';
    else delete layer.dataset.capturing;
    capturing = on; // keeps Svelte's own render of the attribute in agreement
  }

  function probe(): LayoutProbe {
    const rect = orbButton.getBoundingClientRect();
    const root = layer.getRootNode() as Document | ShadowRoot;
    const hit = root.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
    return {
      popoverOpen: layer.matches(':popover-open'),
      viewport: { width: innerWidth, height: innerHeight },
      orb: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      canvas: { width: canvas.width, height: canvas.height, cssWidth: canvas.clientWidth, cssHeight: canvas.clientHeight },
      hitAtOrb: hit && orbButton.contains(hit) ? 'orb' : hit && layer.contains(hit) ? 'companion' : 'page',
    };
  }

  // ---------------------------------------------------------------------------------------------- orb gestures

  function onPointerDown(event: PointerEvent): void {
    if (following || event.button !== 0) return;
    event.preventDefault();
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    drag = {
      pointerId: event.pointerId,
      dx: event.clientX - pos.current.x,
      dy: event.clientY - pos.current.y,
      startX: event.clientX,
      startY: event.clientY,
    };
  }

  function onPointerMove(event: PointerEvent): void {
    if (!drag || event.pointerId !== drag.pointerId) return;
    if (!dragging && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < DRAG_THRESHOLD) return;
    dragging = true;
    void pos.set(
      {
        x: clamp(event.clientX - drag.dx, 0, viewport.width - ORB_SIZE),
        y: clamp(event.clientY - drag.dy, 0, viewport.height - ORB_SIZE),
      },
      { instant: true },
    );
  }

  function onPointerUp(event: PointerEvent): void {
    if (!drag || event.pointerId !== drag.pointerId) return;
    drag = null;
    if (!dragging) return;
    suppressClick = true;
    const at = pos.current;
    dock = { side: at.x + ORB_SIZE / 2 < viewport.width / 2 ? 'left' : 'right', y: at.y };
    dragging = false; // the dock effect springs the orb to the nearest edge
  }

  function onOrbClick(): void {
    if (suppressClick) {
      suppressClick = false;
      return;
    }
    controller.toggleHud();
  }

  /** Follow mode: hover just below-right of the cursor, never under it (the orb ignores the pointer). */
  function onWindowPointerMove(event: PointerEvent): void {
    if (!following || event.pointerType === 'touch') return;
    void pos.set(
      {
        x: clamp(event.clientX + FOLLOW_OFFSET, 4, viewport.width - ORB_SIZE - 4),
        y: clamp(event.clientY + FOLLOW_OFFSET, 4, viewport.height - ORB_SIZE - 4),
      },
      { instant: reduceMotion },
    );
  }

  onMount(() => {
    onSceneSlot(sceneSlot);
    const renderer = new OverlayRenderer(canvas, { reducedMotion: reduceMotion });
    controller.attachView({
      renderer,
      origin: () => ({ x: pos.current.x + ORB_SIZE / 2, y: pos.current.y + ORB_SIZE / 2 }),
      raise: promote,
      setCapturing,
      probe,
    });
    promote();

    const onMotionChange = (): void => {
      reduceMotion = motionQuery.matches;
      renderer.reducedMotion = motionQuery.matches;
    };
    const onResize = (): void => {
      viewport = { width: innerWidth, height: innerHeight };
    };
    const onToggle = (event: Event): void => {
      if (event.target !== layer) queueMicrotask(promote);
    };
    motionQuery.addEventListener('change', onMotionChange);
    addEventListener('resize', onResize, { passive: true });
    addEventListener('pointermove', onWindowPointerMove, { passive: true, capture: true });
    document.addEventListener('toggle', onToggle, true);
    document.addEventListener('fullscreenchange', promote);
    return () => {
      motionQuery.removeEventListener('change', onMotionChange);
      removeEventListener('resize', onResize);
      removeEventListener('pointermove', onWindowPointerMove, { capture: true });
      document.removeEventListener('toggle', onToggle, true);
      document.removeEventListener('fullscreenchange', promote);
      controller.detachView();
      renderer.destroy();
    };
  });
</script>

<div
  bind:this={layer}
  class="cc-layer"
  popover="manual"
  data-visible={$companion.visible}
  data-capturing={capturing ? 'true' : undefined}
>
  <canvas bind:this={canvas} class="cc-overlay" aria-hidden="true"></canvas>
  <div bind:this={sentinelEl} class="cc-sentinel" aria-hidden="true"></div>

  {#if $stageStore.open}
    <ScenePanel
      stage={$stageStore}
      reserve={stageReserve}
      onClose={() => stage.close()}
      onToggleDock={() => stage.toggleDock()}
      attachBody={(body) => stage.attachBody(body)}
      detachBody={() => stage.detachBody()}
    />
  {/if}

  {#if showBubble}
    <div
      class="cc-bubble"
      data-error={$companion.error !== null}
      style:left={bubblePlacement.left}
      style:right={bubblePlacement.right}
      style:top={bubblePlacement.top}
      style:bottom={bubblePlacement.bottom}
      aria-hidden="true"
    >
      {bubbleText}
    </div>
  {/if}

  {#if $trackerStore.open}
    <AgentTracker
      tracker={$trackerStore}
      onStop={() => tracker.stop()}
      onAnswer={(approved) => tracker.answer(approved)}
      onOpen={() => tracker.openInTab()}
      onClose={() => tracker.close()}
    />
  {/if}

  {#if $companion.hudOpen}
    <div
      class="cc-hud-anchor"
      style:left={hudPlacement.left}
      style:right={hudPlacement.right}
      style:top={hudPlacement.top}
      style:bottom={hudPlacement.bottom}
    >
      <Hud
        orb={orbState}
        conn={$companion.conn}
        mode={$companion.mode}
        userText={$companion.userText}
        caption={$companion.caption}
        error={$companion.error}
        busy={$companion.busy}
        timeline={$companion.devTimeline ? $companion.timeline : null}
        {hint}
        onSend={(text) => void controller.sendText(text)}
        onStop={() => void controller.stop()}
        onClose={() => controller.toggleHud(false)}
        onDemo={() => void controller.runDemo()}
        onToggleMode={() => controller.setMode(following ? 'dock' : 'follow')}
      />
    </div>
  {/if}

  <div
    class="cc-orb-anchor"
    data-mode={$companion.mode}
    data-dragging={dragging}
    style:transform={`translate3d(${pos.current.x}px, ${pos.current.y}px, 0)`}
  >
    <button
      bind:this={orbButton}
      type="button"
      class="cc-orb-button"
      tabindex={following ? -1 : 0}
      aria-label={$companion.hudOpen ? `Close the ${APP_NAME} panel` : `Open the ${APP_NAME} panel`}
      aria-expanded={$companion.hudOpen}
      onpointerdown={onPointerDown}
      onpointermove={onPointerMove}
      onpointerup={onPointerUp}
      onpointercancel={onPointerUp}
      onclick={onOrbClick}
    >
      <Orb
        state={orbState}
        conn={$companion.conn}
        {hint}
        level={$companion.level}
        side={orbSide}
        showTip={!$companion.hudOpen && !following && !dragging && !showBubble}
      />
    </button>
  </div>

  <div bind:this={sceneSlot} class="cc-scene-slot"></div>
</div>

<style>
  .cc-layer {
    /* reset the UA popover box: we only want the top-layer behaviour */
    position: fixed;
    inset: 0;
    display: block;
    width: 100vw;
    height: 100vh;
    max-width: none;
    max-height: none;
    margin: 0;
    padding: 0;
    border: 0;
    background: transparent;
    overflow: visible;
    color: var(--cc-text);
    font-family: var(--cc-font);
    z-index: 2147483647;
    pointer-events: none;
  }
  .cc-layer[data-visible='false'] {
    display: none;
  }
  /* Capture: everything but the sentinel turns invisible (opacity keeps focus in the HUD input). */
  .cc-layer[data-capturing='true'] > :not(.cc-sentinel) {
    opacity: 0 !important;
    transition: none !important;
    animation-play-state: paused;
  }
  .cc-sentinel {
    position: fixed;
    left: 0;
    top: 0;
    width: 16px;
    height: 16px;
    display: none;
    pointer-events: none;
  }
  .cc-layer[data-capturing='true'] > .cc-sentinel {
    display: block;
  }

  .cc-overlay {
    position: fixed;
    left: 0;
    top: 0;
    width: 100vw;
    height: 100vh;
    pointer-events: none;
  }

  .cc-orb-anchor {
    position: fixed;
    left: 0;
    top: 0;
    width: 56px;
    height: 56px;
    will-change: transform;
    pointer-events: auto;
  }
  .cc-orb-anchor[data-mode='follow'] {
    pointer-events: none;
  }
  .cc-orb-button {
    display: block;
    width: 100%;
    height: 100%;
    padding: 0;
    margin: 0;
    border: 0;
    border-radius: 50%;
    background: none;
    color: inherit;
    font: inherit;
    cursor: grab;
    touch-action: none;
  }
  .cc-orb-anchor[data-dragging='true'] .cc-orb-button {
    cursor: grabbing;
  }
  .cc-orb-button:focus-visible {
    outline: 2px solid var(--cc-cyan);
    outline-offset: 4px;
  }

  .cc-hud-anchor {
    position: fixed;
    pointer-events: auto;
  }

  .cc-bubble {
    position: fixed;
    max-width: min(300px, calc(100vw - 120px));
    padding: 9px 13px;
    border-radius: 14px;
    background: var(--cc-bg-strong);
    backdrop-filter: blur(var(--cc-blur)) saturate(150%);
    box-shadow: var(--cc-shadow), 0 0 22px rgb(0 240 255 / 0.18);
    color: var(--cc-text);
    font: 13px/1.45 var(--cc-font);
    pointer-events: none;
    animation: cc-bubble-in 0.2s var(--cc-ease);
  }
  .cc-bubble[data-error='true'] {
    box-shadow: var(--cc-shadow), 0 0 22px rgb(255 77 109 / 0.3);
  }

  .cc-scene-slot {
    position: fixed;
    width: 0;
    height: 0;
    overflow: hidden;
  }
  .cc-scene-slot :global(.cc-scene-frame) {
    width: 1px;
    height: 1px;
    border: 0;
    opacity: 0;
  }

  @keyframes cc-bubble-in {
    from {
      opacity: 0;
      transform: translateY(6px);
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .cc-bubble {
      animation: none;
    }
  }
</style>
