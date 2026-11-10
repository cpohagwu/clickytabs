<script lang="ts">
  import { onMount } from 'svelte';
  import { browser } from 'wxt/browser';

  import ConnStatus from '@/components/ConnStatus.svelte';
  import { APP_NAME } from '@/lib/brand';
  import { type ConnInfo, needsPairing } from '@/lib/conn';
  import { DEBUG_TURN_PORT, type DebugTurnEvent, MAX_DEBUG_TURN_CHARS } from '@/lib/messages';
  import { COMPANION_MODE_LABELS, COMPANION_MODES, type CompanionMode } from '@/lib/prefs';
  import { openOnboarding, request, watchConn } from '@/lib/sw-client';

  let conn = $state<ConnInfo | null>(null);
  let text = $state('');
  let output = $state('');
  let turnId: string | null = $state(null);
  let running = $state(false);
  let done: string | null = $state(null);
  let error: string | null = $state(null);
  let port: ReturnType<typeof browser.runtime.connect> | null = null;
  let mode = $state<CompanionMode>('dock');
  let demoError: string | null = $state(null);
  let turnForm: HTMLFormElement;

  function onEvent(event: DebugTurnEvent): void {
    switch (event.type) {
      case 'turn.accepted':
        turnId = event.turn_id;
        running = true;
        return;
      case 'turn.rejected':
        running = false;
        error = event.error;
        return;
      case 'assistant.text.delta':
        if (event.turn_id === turnId) output += event.delta;
        return;
      case 'error':
        if (event.turn_id === turnId || event.turn_id == null) error = event.message;
        return;
      case 'turn.done':
        if (event.turn_id !== turnId) return;
        running = false;
        done = event.reason;
        return;
      default:
        return;
    }
  }

  onMount(() => {
    const stop = watchConn((next) => (conn = next));
    void request({ type: 'settings.read' }).then((prefs) => (mode = prefs.companionMode));
    port = browser.runtime.connect({ name: DEBUG_TURN_PORT });
    port.onMessage.addListener((message: unknown) => onEvent(message as DebugTurnEvent));
    return () => {
      stop();
      port?.disconnect();
    };
  });

  function send(event: SubmitEvent): void {
    event.preventDefault();
    const value = text.trim();
    if (!value || !port) return;
    output = '';
    done = null;
    error = null;
    turnId = null;
    port.postMessage({ type: 'turn.start', text: value.slice(0, MAX_DEBUG_TURN_CHARS) });
  }

  function cancel(): void {
    port?.postMessage({ type: 'turn.cancel' });
  }

  /** Enter sends; Shift+Enter inserts a new line (and Enter while an IME is composing stays with the IME). */
  function onTurnKeydown(event: KeyboardEvent): void {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    turnForm.requestSubmit();
  }

  async function activeTabId(): Promise<number | null> {
    const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
    return tab?.id ?? null;
  }

  /** Outlines the elements the brain gets as marks (e1, e2, …) on the active tab. */
  async function showMarks(): Promise<void> {
    demoError = null;
    const tabId = await activeTabId();
    const reply = tabId === null ? null : await request({ type: 'debug.marks', tabId });
    if (reply?.ok) window.close();
    else demoError = 'The companion is not available on this page (browser pages, the Web Store and PDFs are off limits).';
  }

  /** Plays the scripted pointer tour on the active tab (works without a backend). */
  async function runDemo(): Promise<void> {
    demoError = null;
    const tabId = await activeTabId();
    if (tabId === null) {
      demoError = 'No active tab.';
      return;
    }
    const reply = await request({ type: 'demo.run', tabId });
    if (reply.ok) window.close();
    else demoError = 'The companion is not available on this page (browser pages, the Web Store and PDFs are off limits).';
  }

  async function saveMode(): Promise<void> {
    await request({ type: 'settings.set', patch: { companionMode: mode } });
  }

  /** Follow-up questions continue the conversation; this starts a fresh one. */
  let resetNote: string | null = $state(null);
  const conversationNote = $derived(resetNote ?? 'Follow-ups continue the current conversation.');

  async function newConversation(): Promise<void> {
    const reply = await request({ type: 'conversation.reset' });
    resetNote = reply.ok ? 'Started a new conversation.' : (reply.error ?? 'Could not reset the conversation.');
  }
</script>

<main>
  <header>
    <div class="cc-logo" aria-hidden="true"></div>
    <div>
      <h1>{APP_NAME}</h1>
      <ConnStatus />
    </div>
  </header>

  {#if conn && needsPairing(conn.state)}
    <section class="cc-card">
      <p>Pair this browser with your {APP_NAME} backend to get started.</p>
      <button type="button" data-testid="repair" onclick={() => openOnboarding()}>
        {conn.state === 'unpaired' ? 'Pair now' : 'Re-pair'}
      </button>
    </section>
  {/if}

  <section class="cc-card cc-companion">
    <h2>Companion</h2>
    <div class="cc-row">
      <button type="button" onclick={runDemo} data-testid="demo-run">Tour this page</button>
      <select bind:value={mode} onchange={saveMode} aria-label="Orb placement" data-testid="companion-mode">
        {#each COMPANION_MODES as value (value)}
          <option {value}>{COMPANION_MODE_LABELS[value]}</option>
        {/each}
      </select>
    </div>
    <button type="button" class="cc-secondary" onclick={showMarks} data-testid="show-marks">
      Show what the brain sees
    </button>
    <div class="cc-row">
      <button type="button" class="cc-secondary" disabled={conn?.state !== 'connected'} onclick={newConversation}
        data-testid="conversation-reset">New conversation</button>
      <span class="cc-muted" data-testid="conversation-note">{conversationNote}</span>
    </div>
    {#if demoError}<p class="cc-error">{demoError}</p>{/if}
  </section>

  <section class="cc-card">
    <h2>Text turn <span class="cc-muted">(debug)</span></h2>
    <form bind:this={turnForm} onsubmit={send}>
      <textarea
        rows="2"
        maxlength={MAX_DEBUG_TURN_CHARS}
        placeholder="Ask the brain something… (Enter sends, Shift+Enter for a new line)"
        bind:value={text}
        onkeydown={onTurnKeydown}
        data-testid="turn-input"
      ></textarea>
      <div class="cc-row">
        <button type="submit" disabled={conn?.state !== 'connected' || !text.trim()} data-testid="turn-send">Send</button>
        <button type="button" class="cc-secondary" disabled={!running} onclick={cancel} data-testid="turn-cancel">
          Cancel
        </button>
        {#if done}<span class="cc-pill" data-testid="turn-done" data-reason={done}>{done}</span>{/if}
      </div>
    </form>
    <output data-testid="turn-output" aria-live="polite">{output}</output>
    {#if error}<p class="cc-error" data-testid="turn-error">{error}</p>{/if}
  </section>

  <footer>
    <button type="button" class="cc-secondary" onclick={() => browser.runtime.openOptionsPage()}>Settings</button>
    <button type="button" class="cc-secondary" onclick={() => openOnboarding()}>Pairing &amp; mic</button>
  </footer>
</main>

<style>
  main {
    width: 360px;
    padding: 16px;
    display: grid;
    gap: 12px;
  }
  header {
    display: flex;
    gap: 12px;
    align-items: center;
  }
  h1 {
    font-size: 18px;
  }
  h2 {
    font-size: 14px;
  }
  .cc-logo {
    width: 40px;
    height: 40px;
    border-radius: 50%;
    background:
      radial-gradient(circle at 35% 30%, rgb(255 255 255 / 0.55), transparent 32%),
      conic-gradient(var(--cc-cyan), var(--cc-violet), var(--cc-magenta), var(--cc-cyan));
    box-shadow: var(--cc-glow-cyan);
  }
  form {
    display: grid;
    gap: 8px;
  }
  .cc-row {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  output {
    display: block;
    min-height: 1.5em;
    margin-top: 10px;
    white-space: pre-wrap;
  }
  .cc-error {
    color: var(--cc-red);
    margin: 6px 0 0;
  }
  .cc-companion select {
    flex: 1;
    min-width: 0;
  }
  footer {
    display: flex;
    gap: 8px;
  }
</style>
