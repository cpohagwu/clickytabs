<script lang="ts">
  import { onMount, tick } from 'svelte';

  import { APP_NAME } from '@/lib/brand';
  import { ORB_STATE_LABELS } from '@/lib/companion';
  import { CONN_LABELS, type ConnState } from '@/lib/conn';
  import { MAX_TURN_TEXT_CHARS, type OrbState } from '@/lib/messages';
  import type { CompanionMode } from '@/lib/prefs';
  import { formatMs, type TurnTimeline } from '@/lib/timeline';

  interface Props {
    orb: OrbState;
    conn: ConnState | 'unknown';
    mode: CompanionMode;
    userText: string;
    caption: string;
    error: string | null;
    busy: boolean;
    /** Developer option: where the last answer's time went. */
    timeline?: TurnTimeline | null;
    hint: string;
    onSend: (text: string) => void;
    onStop: () => void;
    onClose: () => void;
    onDemo: () => void;
    onToggleMode: () => void;
  }

  let { orb, conn, mode, userText, caption, error, busy, timeline = null, hint, onSend, onStop, onClose, onDemo,
    onToggleMode }: Props = $props();
  const span = $derived(timeline ? Math.max(...timeline.marks.map((m) => m.ms), 1) : 1);

  let draft = $state('');
  let input: HTMLInputElement;
  let log: HTMLDivElement;

  const connected = $derived(conn === 'connected');
  const status = $derived(orb === 'idle' ? (conn === 'unknown' ? 'Starting…' : CONN_LABELS[conn]) : ORB_STATE_LABELS[orb]);
  const tone = $derived(orb === 'error' ? 'error' : orb !== 'idle' ? 'busy' : connected ? 'ok' : 'attention');

  onMount(() => {
    input?.focus({ preventScroll: true });
  });

  $effect(() => {
    void caption;
    void error;
    void tick().then(() => log?.scrollTo({ top: log.scrollHeight }));
  });

  function submit(event: SubmitEvent): void {
    event.preventDefault();
    const text = draft.trim();
    if (!text || !connected || busy) return;
    draft = '';
    onSend(text);
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
    }
  }
</script>

<section class="cc-hud" aria-label={APP_NAME}>
  <header class="cc-hud-head">
    <span class="cc-hud-status" data-tone={tone}><i></i>{status}</span>
    <div class="cc-hud-tools">
      <button type="button" class="cc-chip" title="Tour this page with the pointer" onclick={onDemo} disabled={busy}>
        Tour
      </button>
      <button
        type="button"
        class="cc-chip"
        title={mode === 'dock' ? 'Let the orb follow your cursor' : 'Dock the orb at the screen edge'}
        onclick={onToggleMode}
      >
        {mode === 'dock' ? 'Follow' : 'Dock'}
      </button>
      <button type="button" class="cc-icon" aria-label="Close panel" title="Close (Esc)" onclick={onClose}>×</button>
    </div>
  </header>

  <div class="cc-hud-log" bind:this={log} aria-live="polite">
    {#if userText}
      <p class="cc-msg cc-you">{userText}</p>
    {/if}
    {#if caption}
      <p class="cc-msg cc-reply">{caption}</p>
    {:else if busy}
      <p class="cc-msg cc-reply cc-typing"><span></span><span></span><span></span></p>
    {/if}
    {#if error}
      <p class="cc-msg cc-error">{error}</p>
    {/if}
    {#if timeline && !busy}
      <div class="cc-timeline" aria-label="Latency of the last answer">
        <div class="cc-timeline-bar" aria-hidden="true">
          {#each timeline.marks as mark (mark.label)}
            <span class="cc-tick" style:left={`${(mark.ms / span) * 100}%`}></span>
          {/each}
        </div>
        <p>
          {timeline.kind === 'voice' ? 'From key release' : 'From Enter'}:
          {#each timeline.marks as mark, i (mark.label)}{i ? ' · ' : ' '}{mark.label} {formatMs(mark.ms)}{/each}
        </p>
      </div>
    {/if}
    {#if !userText && !caption && !error}
      <p class="cc-empty">
        {hint}<br />
        {connected ? 'Or type a question below.' : 'The page tour works offline: press Tour.'}
      </p>
    {/if}
  </div>

  <form class="cc-hud-form" onsubmit={submit}>
    <input
      bind:this={input}
      bind:value={draft}
      onkeydown={onKeydown}
      maxlength={MAX_TURN_TEXT_CHARS}
      placeholder={connected ? 'Ask about this page…' : 'Not connected to the backend'}
      aria-label={`Message ${APP_NAME}`}
      autocomplete="off"
      spellcheck="true"
    />
    {#if busy}
      <button type="button" class="cc-send cc-stop" onclick={onStop}>Stop</button>
    {:else}
      <button type="submit" class="cc-send" disabled={!connected || !draft.trim()}>Send</button>
    {/if}
  </form>
</section>

<style>
  .cc-hud {
    display: grid;
    grid-template-rows: auto minmax(0, 1fr) auto;
    width: min(360px, calc(100vw - 32px));
    max-height: min(420px, calc(100vh - 48px));
    border-radius: var(--cc-radius);
    background: var(--cc-bg);
    backdrop-filter: blur(var(--cc-blur)) saturate(150%);
    box-shadow: var(--cc-shadow), 0 0 32px rgb(124 92 255 / 0.18);
    color: var(--cc-text);
    font: 13px/1.45 var(--cc-font);
    overflow: hidden;
    animation: cc-hud-in 0.22s var(--cc-ease);
  }

  .cc-hud-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 10px 10px 8px 14px;
    border-bottom: 1px solid var(--cc-border);
  }
  .cc-hud-status {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-weight: 600;
    font-size: 12px;
    letter-spacing: 0.02em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .cc-hud-status i {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--cc-amber);
    box-shadow: 0 0 8px currentColor;
  }
  .cc-hud-status[data-tone='ok'] i {
    background: var(--cc-green);
  }
  .cc-hud-status[data-tone='busy'] i {
    background: var(--cc-cyan);
    animation: cc-blink 1s ease-in-out infinite;
  }
  .cc-hud-status[data-tone='error'] i {
    background: var(--cc-red);
  }
  .cc-hud-tools {
    display: flex;
    gap: 6px;
  }

  button {
    font: inherit;
    color: inherit;
    cursor: pointer;
    border: 0;
    background: none;
  }
  button:disabled {
    cursor: default;
    opacity: 0.45;
  }
  button:focus-visible,
  input:focus-visible {
    outline: 2px solid var(--cc-cyan);
    outline-offset: 2px;
  }
  .cc-chip {
    padding: 3px 10px;
    border-radius: 999px;
    border: 1px solid var(--cc-border);
    background: rgb(255 255 255 / 0.06);
    font-size: 11.5px;
    font-weight: 600;
  }
  .cc-chip:not(:disabled):hover {
    border-color: rgb(0 240 255 / 0.5);
    box-shadow: 0 0 12px rgb(0 240 255 / 0.25);
  }
  .cc-icon {
    width: 24px;
    height: 24px;
    border-radius: 50%;
    font-size: 18px;
    line-height: 1;
    color: var(--cc-text-dim);
  }
  .cc-icon:hover {
    color: var(--cc-text);
    background: rgb(255 255 255 / 0.08);
  }

  .cc-hud-log {
    display: grid;
    align-content: start;
    gap: 8px;
    padding: 12px 14px;
    min-height: 72px;
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-width: thin;
    scrollbar-color: rgb(255 255 255 / 0.2) transparent;
  }
  .cc-msg {
    margin: 0;
    padding: 8px 11px;
    border-radius: 12px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .cc-you {
    justify-self: end;
    max-width: 85%;
    background: linear-gradient(135deg, rgb(124 92 255 / 0.45), rgb(255 43 214 / 0.3));
  }
  .cc-reply {
    justify-self: start;
    max-width: 95%;
    background: rgb(255 255 255 / 0.07);
    border: 1px solid rgb(0 240 255 / 0.18);
  }
  .cc-error {
    background: rgb(255 77 109 / 0.16);
    border: 1px solid rgb(255 77 109 / 0.45);
  }
  .cc-empty {
    margin: 0;
    color: var(--cc-text-dim);
  }
  .cc-typing {
    display: inline-flex;
    gap: 4px;
  }
  .cc-typing span {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--cc-cyan);
    animation: cc-blink 1s ease-in-out infinite;
  }
  .cc-typing span:nth-child(2) {
    animation-delay: 0.15s;
  }
  .cc-typing span:nth-child(3) {
    animation-delay: 0.3s;
  }

  .cc-hud-form {
    display: flex;
    gap: 8px;
    margin: 0;
    padding: 10px;
    border-top: 1px solid var(--cc-border);
  }
  input {
    flex: 1;
    min-width: 0;
    padding: 8px 12px;
    border-radius: 10px;
    border: 1px solid var(--cc-border);
    background: rgb(0 0 0 / 0.28);
    color: var(--cc-text);
    font: inherit;
  }
  input::placeholder {
    color: var(--cc-text-dim);
  }
  .cc-send {
    padding: 0 14px;
    border-radius: 10px;
    font-weight: 700;
    background: linear-gradient(135deg, var(--cc-cyan), var(--cc-violet));
    color: #07101c;
  }
  .cc-stop {
    background: linear-gradient(135deg, var(--cc-magenta), var(--cc-red));
    color: #fff;
  }

  @keyframes cc-hud-in {
    from {
      opacity: 0;
      transform: translateY(8px) scale(0.98);
    }
  }
  @keyframes cc-blink {
    50% {
      opacity: 0.3;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .cc-hud,
    .cc-typing span,
    .cc-hud-status i {
      animation: none !important;
    }
  }
  .cc-timeline {
    margin-top: 6px;
    font-size: 11px;
    color: var(--cc-text-dim);
  }
  .cc-timeline p {
    margin: 4px 0 0;
  }
  .cc-timeline-bar {
    position: relative;
    height: 4px;
    border-radius: 2px;
    background: color-mix(in srgb, var(--cc-text-dim) 25%, transparent);
  }
  .cc-tick {
    position: absolute;
    top: -2px;
    width: 2px;
    height: 8px;
    margin-left: -1px;
    border-radius: 1px;
    background: var(--cc-cyan);
  }
</style>
