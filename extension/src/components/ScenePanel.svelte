<script lang="ts">
  /**
   * The scene stage: a glass panel in front of the page holding the sandboxed scene frame, labelled
   * "AI-generated". It can be moved (drag the header), resized (corner grip), pinned to the side and closed.
   * Positions go through style: directives (CSSOM), never style="" strings (strict page CSPs block those).
   */
  import { onDestroy, onMount } from 'svelte';

  import type { StageState } from '@/lib/scene/stage';

  interface Props {
    stage: StageState;
    /** Room to leave free on one side for the companion (orb + HUD). */
    reserve: { side: 'left' | 'right'; width: number };
    onClose: () => void;
    onToggleDock: () => void;
    attachBody: (body: HTMLElement) => void;
    detachBody: () => void;
  }

  let { stage, reserve, onClose, onToggleDock, attachBody, detachBody }: Props = $props();

  const MIN_W = 300;
  const MIN_H = 220;
  const DOCKED = { width: 420, height: 300 };
  const MARGIN = 24;
  const MIN_FREE = 420;           // below this, the stage may overlap the companion rather than shrink

  let body: HTMLDivElement;
  let viewport = $state({ width: innerWidth, height: innerHeight });
  let size = $state({ width: Math.min(760, innerWidth - 48), height: Math.min(500, innerHeight - 140) });
  let offset = $state({ x: 0, y: 0 });
  let now = $state(Date.now());
  let gesture: { kind: 'move' | 'resize'; id: number; x: number; y: number; start: { x: number; y: number } } | null = null;
  let interacting = $state(false);

  const busy = $derived(stage.status === 'building' || stage.status === 'rendering' || stage.status === 'repairing');
  const elapsed = $derived(Math.max(0, Math.round((now - stage.startedAt) / 1000)));
  const statusText = $derived(
    stage.status === 'building' ? `Writing the scene… ${elapsed}s`
      : stage.status === 'rendering' ? 'Rendering…'
        : stage.status === 'repairing' ? 'Fixing an error…'
          : stage.status === 'failed' ? 'Could not build this scene'
            : stage.kind === 'three' ? 'Drag to orbit · scroll to zoom' : '',
  );

  const box = $derived.by(() => {
    const free = viewport.width - reserve.width >= MIN_FREE;
    const lo = free && reserve.side === 'left' ? reserve.width : 0;
    const hi = free && reserve.side === 'right' ? viewport.width - reserve.width : viewport.width;
    const width = Math.min(stage.docked ? DOCKED.width : size.width, hi - lo - 16);
    const height = Math.min(stage.docked ? DOCKED.height : size.height, viewport.height - 16);
    // Pinned: the corner away from the companion; otherwise centred in the free area.
    const baseLeft = stage.docked
      ? (reserve.side === 'left' ? viewport.width - width - MARGIN : MARGIN)
      : lo + (hi - lo - width) / 2;
    const baseTop = stage.docked ? viewport.height - height - MARGIN : (viewport.height - height) / 2 - 20;
    const left = Math.min(Math.max(8, baseLeft + offset.x), viewport.width - width - 8);
    const top = Math.min(Math.max(8, baseTop + offset.y), viewport.height - height - 8);
    return { left, top, width, height };
  });

  $effect(() => {
    if (!busy) return;
    const timer = setInterval(() => (now = Date.now()), 500);
    return () => clearInterval(timer);
  });

  function begin(kind: 'move' | 'resize', event: PointerEvent): void {
    if (event.button !== 0) return;
    if (kind === 'move' && (event.target as HTMLElement).closest('button')) return;
    event.preventDefault();
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    gesture = {
      kind, id: event.pointerId, x: event.clientX, y: event.clientY,
      start: kind === 'move' ? { ...offset } : { x: box.width, y: box.height },
    };
    interacting = true; // the frame must not swallow the pointer while dragging
  }

  function move(event: PointerEvent): void {
    if (!gesture || event.pointerId !== gesture.id) return;
    const dx = event.clientX - gesture.x;
    const dy = event.clientY - gesture.y;
    if (gesture.kind === 'move') {
      offset = { x: gesture.start.x + dx, y: gesture.start.y + dy };
    } else {
      size = {
        width: Math.max(MIN_W, Math.min(viewport.width - 16, gesture.start.x + dx * (stage.docked ? 1 : 2))),
        height: Math.max(MIN_H, Math.min(viewport.height - 16, gesture.start.y + dy * (stage.docked ? 1 : 2))),
      };
    }
  }

  function end(event: PointerEvent): void {
    if (!gesture || event.pointerId !== gesture.id) return;
    gesture = null;
    interacting = false;
  }

  function dock(): void {
    offset = { x: 0, y: 0 };
    onToggleDock();
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
    }
  }

  onMount(() => {
    attachBody(body);
    const onResize = (): void => {
      viewport = { width: innerWidth, height: innerHeight };
    };
    addEventListener('resize', onResize, { passive: true });
    return () => removeEventListener('resize', onResize);
  });
  onDestroy(() => detachBody());
</script>

<!-- Escape closes the stage while focus is inside it (keys never reach the page: the shadow root isolates them). -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<section
  class="cc-stage"
  data-status={stage.status}
  data-docked={stage.docked}
  data-interacting={interacting}
  aria-label={`AI-generated scene: ${stage.title}`}
  style:left={`${box.left}px`}
  style:top={`${box.top}px`}
  style:width={`${box.width}px`}
  style:height={`${box.height}px`}
  onkeydown={onKeydown}
>
  <!-- The header is a drag handle for pointers; the buttons inside are the keyboard-accessible controls. -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <header
    class="cc-stage-head"
    onpointerdown={(e) => begin('move', e)}
    onpointermove={move}
    onpointerup={end}
    onpointercancel={end}
  >
    <div class="cc-stage-title">
      <span class="cc-ai" title="Written by an AI model for this answer; it may contain mistakes">AI-generated</span>
      <strong>{stage.title || 'Scene'}</strong>
    </div>
    <span class="cc-stage-status" data-busy={busy}>{statusText}</span>
    <div class="cc-stage-tools">
      <button
        type="button"
        class="cc-icon"
        aria-label={stage.docked ? 'Bring the scene to the front' : 'Pin the scene to the side'}
        title={stage.docked ? 'Bring to the front' : 'Pin to the side'}
        aria-pressed={stage.docked}
        onclick={dock}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 3l6 6-3 1-4 4 1 5-2 2-4-4-5 5-1-1 5-5-4-4 2-2 5 1 4-4z" /></svg>
      </button>
      <button type="button" class="cc-icon" aria-label="Close the scene" title="Close (Esc)" onclick={onClose}>×</button>
    </div>
  </header>

  <div class="cc-stage-body" bind:this={body}>
    {#if stage.status !== 'shown'}
      <div class="cc-stage-cover" data-status={stage.status} aria-live="polite">
        {#if stage.status === 'failed'}
          <p class="cc-stage-error">{stage.error ?? 'Something went wrong.'}</p>
          <button type="button" class="cc-chip" onclick={onClose}>Close</button>
        {:else}
          <div class="cc-stage-spinner" aria-hidden="true"><i></i><i></i><i></i></div>
          <p>{statusText}</p>
        {/if}
      </div>
    {:else if stage.error}
      <p class="cc-stage-late">{stage.error}</p>
    {/if}
  </div>

  <div
    class="cc-stage-grip"
    role="separator"
    aria-label="Resize the scene"
    onpointerdown={(e) => begin('resize', e)}
    onpointermove={move}
    onpointerup={end}
    onpointercancel={end}
  ></div>
</section>

<style>
  .cc-stage {
    position: fixed;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    border-radius: calc(var(--cc-radius) + 4px);
    background:
      radial-gradient(120% 90% at 50% 0%, rgb(124 92 255 / 0.22), transparent 60%),
      linear-gradient(180deg, rgb(12 16 34 / 0.94), rgb(8 10 22 / 0.96));
    backdrop-filter: blur(var(--cc-blur)) saturate(150%);
    box-shadow: var(--cc-shadow), 0 0 0 1px rgb(0 240 255 / 0.18), 0 0 48px rgb(124 92 255 / 0.28);
    color: var(--cc-text);
    font: 13px/1.45 var(--cc-font);
    overflow: hidden;
    pointer-events: auto;
    animation: cc-stage-in 0.32s var(--cc-ease);
  }

  .cc-stage-head {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 8px 8px 14px;
    border-bottom: 1px solid var(--cc-border);
    cursor: grab;
    touch-action: none;
    user-select: none;
  }
  .cc-stage[data-interacting='true'] .cc-stage-head {
    cursor: grabbing;
  }
  .cc-stage-title {
    display: flex;
    align-items: center;
    gap: 9px;
    min-width: 0;
  }
  .cc-stage-title strong {
    font-size: 13.5px;
    font-weight: 650;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .cc-ai {
    flex: none;
    padding: 2px 8px;
    border-radius: 999px;
    border: 1px solid rgb(255 43 214 / 0.45);
    background: rgb(255 43 214 / 0.12);
    color: #ffc2f2;
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .cc-stage-status {
    margin-left: auto;
    color: var(--cc-text-dim);
    font-size: 11.5px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .cc-stage-status[data-busy='true'] {
    color: var(--cc-cyan);
  }
  .cc-stage-tools {
    display: flex;
    flex: none;
    gap: 2px;
  }

  button {
    font: inherit;
    color: inherit;
    cursor: pointer;
    border: 0;
    background: none;
  }
  button:focus-visible {
    outline: 2px solid var(--cc-cyan);
    outline-offset: 2px;
  }
  .cc-icon {
    display: grid;
    place-items: center;
    width: 26px;
    height: 26px;
    border-radius: 50%;
    font-size: 18px;
    line-height: 1;
    color: var(--cc-text-dim);
  }
  .cc-icon:hover,
  .cc-icon[aria-pressed='true'] {
    color: var(--cc-text);
    background: rgb(255 255 255 / 0.08);
  }
  .cc-icon svg {
    width: 15px;
    height: 15px;
    fill: currentColor;
  }
  .cc-chip {
    padding: 4px 12px;
    border-radius: 999px;
    border: 1px solid var(--cc-border);
    background: rgb(255 255 255 / 0.06);
    font-size: 12px;
    font-weight: 600;
  }

  .cc-stage-body {
    position: relative;
    min-height: 0;
  }
  .cc-stage-body :global(.cc-scene-frame) {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    border: 0;
    background: transparent;
    color-scheme: dark;
  }
  .cc-stage[data-interacting='true'] .cc-stage-body :global(.cc-scene-frame) {
    pointer-events: none;
  }
  .cc-stage-cover {
    position: absolute;
    inset: 0;
    z-index: 1;
    display: grid;
    place-content: center;
    justify-items: center;
    gap: 14px;
    padding: 24px;
    text-align: center;
    color: var(--cc-text-dim);
    background: linear-gradient(180deg, rgb(12 16 34 / 0.6), rgb(8 10 22 / 0.85));
  }
  .cc-stage-cover p {
    margin: 0;
  }
  .cc-stage-error {
    max-width: 460px;
    color: #ffc0cb;
    overflow-wrap: anywhere;
  }
  .cc-stage-late {
    position: absolute;
    left: 12px;
    right: 12px;
    bottom: 12px;
    z-index: 1;
    margin: 0;
    padding: 6px 10px;
    border-radius: 10px;
    background: rgb(255 77 109 / 0.18);
    color: #ffc0cb;
    font-size: 12px;
  }
  .cc-stage-spinner {
    display: flex;
    gap: 7px;
  }
  .cc-stage-spinner i {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--cc-cyan);
    box-shadow: var(--cc-glow-cyan);
    animation: cc-stage-dot 1.1s ease-in-out infinite;
  }
  .cc-stage-spinner i:nth-child(2) {
    animation-delay: 0.15s;
    background: var(--cc-violet);
  }
  .cc-stage-spinner i:nth-child(3) {
    animation-delay: 0.3s;
    background: var(--cc-magenta);
  }

  .cc-stage-grip {
    position: absolute;
    right: 0;
    bottom: 0;
    z-index: 2;
    width: 18px;
    height: 18px;
    cursor: nwse-resize;
    touch-action: none;
    background: linear-gradient(135deg, transparent 50%, rgb(255 255 255 / 0.25) 50%, transparent 62%,
      rgb(255 255 255 / 0.25) 62%, transparent 74%);
  }

  @keyframes cc-stage-in {
    from {
      opacity: 0;
      transform: translateY(12px) scale(0.97);
    }
  }
  @keyframes cc-stage-dot {
    0%,
    100% {
      transform: translateY(0);
      opacity: 0.5;
    }
    50% {
      transform: translateY(-7px);
      opacity: 1;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .cc-stage,
    .cc-stage-spinner i {
      animation: none;
    }
  }
</style>
