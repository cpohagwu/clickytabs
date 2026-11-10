<script lang="ts">
  import { APP_NAME } from '@/lib/brand';
  import { ORB_STATE_LABELS } from '@/lib/companion';
  import { CONN_LABELS, type ConnState } from '@/lib/conn';
  import type { OrbState } from '@/lib/messages';

  interface Props {
    state: OrbState;
    conn: ConnState | 'unknown';
    hint: string;
    size?: number;
    /** 0..1 voice envelope (audio-reactive glow). */
    level?: number;
    /** Which side of the screen the orb rests on: the hover tip opens toward the page. */
    side?: 'left' | 'right';
    showTip?: boolean;
  }

  let { state, conn, hint, size = 56, level = 0, side = 'right', showTip = true }: Props = $props();

  const connLabel = $derived(conn === 'unknown' ? 'Starting…' : CONN_LABELS[conn]);
  const title = $derived(state === 'idle' ? connLabel : ORB_STATE_LABELS[state]);
  const tone = $derived(
    conn === 'connected' ? 'ok' : conn === 'connecting' || conn === 'unknown' ? 'busy' : 'attention',
  );
</script>

<div
  class="cc-orb"
  data-state={state}
  data-conn={conn}
  data-side={side}
  style:--cc-orb-size={`${size}px`}
  style:--cc-level={level.toFixed(3)}
  role="status"
  aria-live="polite"
  aria-label={`${APP_NAME}: ${connLabel}${state === 'idle' ? '' : `, ${ORB_STATE_LABELS[state].toLowerCase()}`}`}
>
  <div class="cc-orb-body">
    <div class="cc-orb-halo"></div>
    <div class="cc-orb-ring"></div>
    <div class="cc-orb-core"></div>
    <div class="cc-orb-gloss"></div>
  </div>
  <span class="cc-orb-dot" data-tone={tone}></span>
  {#if showTip}
    <div class="cc-orb-tip" aria-hidden="true">
      <strong>{title}</strong>
      <span>{hint}</span>
    </div>
  {/if}
</div>

<style>
  .cc-orb {
    --cc-level: 0;
    position: relative;
    width: var(--cc-orb-size);
    height: var(--cc-orb-size);
    border-radius: 50%;
    isolation: isolate;
    animation: cc-breathe 4.8s var(--cc-ease) infinite;
  }

  /* audio-reactive scale lives on its own element so it composes with the breathing animation */
  .cc-orb-body {
    position: absolute;
    inset: 0;
    transform: scale(calc(1 + var(--cc-level) * 0.14));
    transition: transform 90ms linear;
  }

  .cc-orb-halo {
    position: absolute;
    inset: -10px;
    border-radius: 50%;
    background: radial-gradient(circle, rgb(0 240 255 / 0.35), rgb(124 92 255 / 0.12) 55%, transparent 70%);
    filter: blur(6px);
    opacity: calc(0.75 + var(--cc-level) * 0.25);
    transform: scale(calc(1 + var(--cc-level) * 0.45));
    transition:
      background 0.4s var(--cc-ease),
      transform 90ms linear;
    z-index: -1;
  }

  .cc-orb-ring {
    position: absolute;
    inset: -5px;
    border-radius: 50%;
    border: 2px solid transparent;
    opacity: 0;
    transition: opacity 0.3s var(--cc-ease);
  }

  .cc-orb-core {
    position: absolute;
    inset: 0;
    border-radius: 50%;
    background:
      radial-gradient(circle at 35% 30%, rgb(255 255 255 / 0.55), transparent 32%),
      conic-gradient(from 0deg, var(--cc-cyan), var(--cc-violet), var(--cc-magenta), var(--cc-cyan));
    box-shadow: var(--cc-glow-cyan), inset 0 0 18px rgb(0 0 0 / 0.45);
    animation: cc-spin 9s linear infinite;
    transition: filter 0.3s var(--cc-ease);
  }

  .cc-orb-gloss {
    position: absolute;
    inset: 3px;
    border-radius: 50%;
    background: radial-gradient(circle at 50% 120%, rgb(10 12 24 / 0.65), transparent 60%);
    border: 1px solid rgb(255 255 255 / 0.25);
  }

  .cc-orb-dot {
    position: absolute;
    right: 1px;
    bottom: 1px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    border: 2px solid var(--cc-bg-strong);
    background: var(--cc-amber);
  }
  .cc-orb-dot[data-tone='ok'] {
    background: var(--cc-green);
    box-shadow: 0 0 8px var(--cc-green);
  }
  .cc-orb-dot[data-tone='attention'] {
    background: var(--cc-red);
    box-shadow: 0 0 8px var(--cc-red);
  }

  /* ---------------------------------------------------------------------------------------------- states */

  .cc-orb[data-state='listening'] {
    animation: cc-pulse 0.9s var(--cc-ease) infinite;
  }
  .cc-orb[data-state='listening'] .cc-orb-core {
    box-shadow: var(--cc-glow-magenta), inset 0 0 18px rgb(0 0 0 / 0.4);
    animation-duration: 2.4s;
  }
  .cc-orb[data-state='listening'] .cc-orb-halo {
    background: radial-gradient(circle, rgb(255 43 214 / 0.5), rgb(124 92 255 / 0.18) 55%, transparent 72%);
  }

  .cc-orb[data-state='thinking'] .cc-orb-core {
    animation-duration: 1.4s;
  }
  .cc-orb[data-state='thinking'] .cc-orb-ring {
    opacity: 1;
    border-top-color: var(--cc-cyan);
    border-right-color: rgb(124 92 255 / 0.6);
    animation: cc-spin 0.9s linear infinite;
  }

  .cc-orb[data-state='speaking'] .cc-orb-core {
    animation-duration: 3.2s;
    box-shadow:
      0 0 calc(14px + var(--cc-level) * 26px) rgb(0 240 255 / 0.6),
      0 0 calc(30px + var(--cc-level) * 40px) rgb(124 92 255 / 0.4),
      inset 0 0 18px rgb(0 0 0 / 0.4);
  }
  .cc-orb[data-state='speaking'] .cc-orb-halo {
    background: radial-gradient(circle, rgb(0 240 255 / 0.5), rgb(124 92 255 / 0.22) 55%, transparent 72%);
  }

  .cc-orb[data-state='acting'] .cc-orb-ring {
    opacity: 1;
    border-color: rgb(57 245 160 / 0.25);
    border-top-color: var(--cc-green);
    animation: cc-spin 1.3s linear infinite;
  }
  .cc-orb[data-state='acting'] .cc-orb-core {
    animation-duration: 4s;
  }

  .cc-orb[data-state='error'] {
    animation: cc-shake 0.42s ease-in-out 1;
  }
  .cc-orb[data-state='error'] .cc-orb-core {
    filter: hue-rotate(-60deg) saturate(1.3);
    box-shadow: 0 0 18px rgb(255 77 109 / 0.65), inset 0 0 18px rgb(0 0 0 / 0.45);
  }
  .cc-orb[data-state='error'] .cc-orb-halo {
    background: radial-gradient(circle, rgb(255 77 109 / 0.45), transparent 70%);
  }

  .cc-orb[data-conn='unauthorized'] .cc-orb-core,
  .cc-orb[data-conn='unpaired'] .cc-orb-core,
  .cc-orb[data-conn='disconnected'] .cc-orb-core {
    filter: saturate(0.35) brightness(0.8);
  }

  /* ---------------------------------------------------------------------------------------------- tip */

  .cc-orb-tip {
    position: absolute;
    right: calc(100% + 12px);
    bottom: 4px;
    display: grid;
    gap: 2px;
    min-width: 180px;
    max-width: 260px;
    padding: 10px 12px;
    border-radius: 12px;
    background: var(--cc-bg-strong);
    backdrop-filter: blur(var(--cc-blur)) saturate(140%);
    box-shadow: var(--cc-shadow);
    color: var(--cc-text);
    font: 12px/1.35 var(--cc-font);
    opacity: 0;
    transform: translateX(6px);
    pointer-events: none;
    transition:
      opacity 0.2s var(--cc-ease),
      transform 0.2s var(--cc-ease);
  }
  .cc-orb[data-side='left'] .cc-orb-tip {
    right: auto;
    left: calc(100% + 12px);
    transform: translateX(-6px);
  }
  .cc-orb-tip span {
    color: var(--cc-text-dim);
  }
  .cc-orb:hover .cc-orb-tip {
    opacity: 1;
    transform: none;
  }

  @keyframes cc-spin {
    to {
      transform: rotate(360deg);
    }
  }
  @keyframes cc-breathe {
    0%,
    100% {
      transform: scale(1);
    }
    50% {
      transform: scale(1.045);
    }
  }
  @keyframes cc-pulse {
    0%,
    100% {
      transform: scale(1);
    }
    50% {
      transform: scale(1.12);
    }
  }
  @keyframes cc-shake {
    0%,
    100% {
      transform: translateX(0);
    }
    25% {
      transform: translateX(-4px);
    }
    75% {
      transform: translateX(4px);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .cc-orb,
    .cc-orb-core,
    .cc-orb-ring {
      animation: none !important;
    }
    .cc-orb-body,
    .cc-orb-halo {
      transform: none;
    }
  }
</style>
