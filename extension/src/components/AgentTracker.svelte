<script lang="ts">
  /**
   * The browser-task tracker: what the agent is doing in its own browser, its questions before irreversible
   * steps (Approve / Decline need a real click), Stop, and "Open in my tab" when it is done. Top-right, out of the
   * way of the orb, the HUD and the scene stage.
   */
  import type { TrackerState } from '@/lib/task/tracker';

  interface Props {
    tracker: TrackerState;
    onStop: () => void;
    onAnswer: (approved: boolean) => void;
    onOpen: () => void;
    onClose: () => void;
  }

  let { tracker, onStop, onAnswer, onOpen, onClose }: Props = $props();

  const finished = $derived(tracker.status === 'done' || tracker.status === 'failed');
  const statusText = $derived(
    tracker.status === 'consent' ? 'Waiting for your OK in the consent window'
      : tracker.status === 'starting' ? 'Starting a browser…'
        : tracker.status === 'running' ? 'Working…'
          : tracker.status === 'confirm' ? 'Needs your OK'
            : tracker.status === 'done' ? 'Done' : 'Stopped',
  );
  const access = $derived(
    tracker.session ? `${tracker.sites.join(', ')} · using your login${tracker.cookies ? ` (${tracker.cookies} ${tracker.cookies === 1 ? 'cookie' : 'cookies'})` : ''}`
      : `${tracker.sites.join(', ')} · without your login`,
  );

  /** Only real clicks count: a page script can't approve an order through the shadow root, but be strict anyway. */
  function trusted(handler: () => void) {
    return (event: MouseEvent) => {
      if (event.isTrusted) handler();
    };
  }
</script>

<section class="cc-task" data-status={tracker.status} aria-label="Browser task" aria-live="polite">
  <header>
    <span class="cc-chip-agent">Browser task</span>
    <span class="cc-task-status" data-busy={!finished && tracker.status !== 'confirm'}>{statusText}</span>
    {#if finished}
      <button type="button" class="cc-icon" aria-label="Close the task tracker" onclick={onClose}>×</button>
    {/if}
  </header>

  <p class="cc-task-goal" title={tracker.goal}>{tracker.goal}</p>
  {#if tracker.sites.length}
    <p class="cc-task-access">{access}{tracker.auto ? ' · allowed before (Always)' : ''}</p>
  {/if}

  {#if tracker.thumb}
    <img class="cc-task-thumb" alt="What the agent's browser shows" src={`data:image/jpeg;base64,${tracker.thumb}`} />
  {/if}

  {#if tracker.steps.length}
    <ol class="cc-task-steps">
      {#each tracker.steps as step (step.n)}
        <li value={step.n}>{step.text}</li>
      {/each}
    </ol>
  {/if}

  {#if tracker.confirm}
    <div class="cc-task-confirm" role="alertdialog" aria-label="The agent asks before going on">
      <p>{tracker.confirm.question}</p>
      <div class="cc-task-actions">
        <button type="button" class="cc-btn cc-approve" data-testid="task-approve" onclick={trusted(() => onAnswer(true))}>
          Approve
        </button>
        <button type="button" class="cc-btn" data-testid="task-decline" onclick={trusted(() => onAnswer(false))}>Decline</button>
      </div>
    </div>
  {/if}

  {#if finished}
    <p class="cc-task-summary" data-success={tracker.status === 'done'}>{tracker.summary}</p>
    {#if tracker.finalUrl && tracker.status === 'done'}
      <div class="cc-task-actions">
        <button type="button" class="cc-btn cc-approve" onclick={trusted(onOpen)}>Open in my tab</button>
      </div>
      <p class="cc-task-note">The agent used a copy of your login. What it changed on the site is there; cookies it got
        stay in its browser.</p>
    {/if}
  {:else}
    <div class="cc-task-actions">
      <button type="button" class="cc-btn cc-stop" onclick={trusted(onStop)}>Stop</button>
    </div>
  {/if}
</section>

<style>
  .cc-task {
    position: fixed;
    top: 20px;
    right: 20px;
    display: grid;
    gap: 8px;
    width: min(340px, calc(100vw - 40px));
    max-height: calc(100vh - 140px);
    overflow-y: auto;
    padding: 12px 14px 14px;
    border-radius: var(--cc-radius);
    background: var(--cc-bg-strong);
    backdrop-filter: blur(var(--cc-blur)) saturate(150%);
    box-shadow: var(--cc-shadow), 0 0 32px rgb(0 240 255 / 0.16);
    color: var(--cc-text);
    font: 13px/1.45 var(--cc-font);
    pointer-events: auto;
    animation: cc-task-in 0.25s var(--cc-ease);
  }
  .cc-task[data-status='confirm'] {
    box-shadow: var(--cc-shadow), 0 0 0 1px rgb(245 165 36 / 0.6), 0 0 36px rgb(245 165 36 / 0.25);
  }
  header {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .cc-chip-agent {
    padding: 2px 8px;
    border-radius: 999px;
    border: 1px solid rgb(0 240 255 / 0.45);
    background: rgb(0 240 255 / 0.1);
    color: #b8faff;
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .cc-task-status {
    flex: 1;
    color: var(--cc-text-dim);
    font-size: 12px;
  }
  .cc-task-status[data-busy='true'] {
    color: var(--cc-cyan);
  }
  p {
    margin: 0;
  }
  .cc-task-goal {
    font-weight: 600;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .cc-task-access,
  .cc-task-note {
    color: var(--cc-text-dim);
    font-size: 11.5px;
  }
  .cc-task-thumb {
    width: 100%;
    border-radius: 10px;
    border: 1px solid var(--cc-border);
  }
  .cc-task-steps {
    margin: 0;
    padding-left: 22px;
    display: grid;
    gap: 3px;
    color: var(--cc-text);
    font-size: 12.5px;
  }
  .cc-task-steps li:last-child {
    color: var(--cc-cyan);
  }
  .cc-task-confirm {
    display: grid;
    gap: 8px;
    padding: 10px;
    border-radius: 12px;
    background: rgb(245 165 36 / 0.12);
    border: 1px solid rgb(245 165 36 / 0.45);
  }
  .cc-task-summary {
    padding: 8px 10px;
    border-radius: 10px;
    background: rgb(255 255 255 / 0.06);
  }
  .cc-task-summary[data-success='false'] {
    background: rgb(255 77 109 / 0.14);
  }
  .cc-task-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
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
  .cc-btn {
    padding: 5px 14px;
    border-radius: 999px;
    border: 1px solid var(--cc-border);
    background: rgb(255 255 255 / 0.07);
    font-weight: 600;
    font-size: 12.5px;
  }
  .cc-approve {
    border-color: rgb(57 245 160 / 0.6);
    background: rgb(57 245 160 / 0.16);
  }
  .cc-stop {
    border-color: rgb(255 77 109 / 0.55);
    background: rgb(255 77 109 / 0.14);
  }
  .cc-icon {
    width: 24px;
    height: 24px;
    border-radius: 50%;
    font-size: 18px;
    line-height: 1;
    color: var(--cc-text-dim);
  }
  @keyframes cc-task-in {
    from {
      opacity: 0;
      transform: translateY(-8px);
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .cc-task {
      animation: none;
    }
  }
</style>
