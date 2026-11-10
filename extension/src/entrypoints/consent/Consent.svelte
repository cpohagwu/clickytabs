<script lang="ts">
  /**
   * The consent window for a browser task. An extension popup window (not part of the page, so a page can't
   * overlay or clickjack it). It shows the goal verbatim, the sites, and whether the login is shared (and how many
   * cookies); the buttons arm after a short delay and only accept real clicks. "Always" is offered only for a task
   * that stays on the tab's own site.
   */
  import { onMount } from 'svelte';

  import { APP_NAME } from '@/lib/brand';
  import type { ConsentView } from '@/lib/messages';
  import { request } from '@/lib/sw-client';

  const ARM_MS = 1500;
  const taskId = new URLSearchParams(location.search).get('task') ?? '';

  let view = $state<ConsentView | null>(null);
  let error = $state<string | null>(null);
  let armed = $state(false);
  let sending = $state(false);

  onMount(() => {
    void request({ type: 'consent.get', taskId }).then((reply) => {
      if (!reply || 'ok' in reply) error = reply && 'error' in reply ? reply.error : 'This request is no longer open.';
      else view = reply;
    });
    const timer = setTimeout(() => (armed = true), ARM_MS);
    return () => clearTimeout(timer);
  });

  async function decide(event: MouseEvent, decision: 'allow_once' | 'always' | 'deny'): Promise<void> {
    if (!event.isTrusted || (!armed && decision !== 'deny') || sending) return;
    sending = true;
    await request({ type: 'consent.decide', taskId, decision });
    window.close();
  }
</script>

<main class="consent">
  <h1>Let {APP_NAME} do this in a browser?</h1>
  {#if error}
    <p class="error">{error}</p>
    <button type="button" class="btn" onclick={() => window.close()}>Close</button>
  {:else if view}
    <section class="goal" aria-label="The task, as the assistant wrote it">
      <p class="label">The task</p>
      <blockquote data-testid="consent-goal">{view.goal}</blockquote>
    </section>

    <dl class="facts">
      <dt>Your tab</dt>
      <dd data-testid="consent-tab-site">{view.tabSite}{view.tabTitle ? ` · ${view.tabTitle}` : ''}</dd>
      {#if view.crossSite}
        <dt>Acts on</dt>
        <dd class="warn" data-testid="consent-task-site">{view.taskSite} (a different site)</dd>
      {/if}
      <dt>Your login</dt>
      <dd data-testid="consent-session">
        {view.session
          ? `Shared: a copy of your ${view.tabSite} cookies (${view.cookieCount}) and its saved site data`
          : 'Not shared: the agent starts signed out'}
      </dd>
    </dl>

    <p class="how">A separate browser window does it, step by step, and stops to ask you before anything that can't be
      undone, like paying or sending. You can stop it at any time from your tab. Its copy of your login is deleted
      when it finishes.</p>
    {#if view.crossSite}
      <p class="warn-box">This task goes to a different site from the one you're on. Check that you asked for this.</p>
    {/if}

    <div class="actions">
      <button type="button" class="btn deny" data-testid="consent-deny" onclick={(e) => decide(e, 'deny')}>Don't allow</button>
      {#if !view.crossSite}
        <button type="button" class="btn" data-testid="consent-always" disabled={!armed || sending}
          onclick={(e) => decide(e, 'always')}>Always allow on {view.tabSite}</button>
      {/if}
      <button type="button" class="btn allow" data-testid="consent-allow" disabled={!armed || sending}
        onclick={(e) => decide(e, 'allow_once')}>Allow once</button>
    </div>
  {:else}
    <p>Loading…</p>
  {/if}
</main>

<style>
  .consent {
    display: grid;
    gap: 14px;
    max-width: 520px;
    margin: 0 auto;
    padding: 22px 24px 24px;
  }
  h1 {
    margin: 0;
    font-size: 18px;
  }
  .label {
    margin: 0 0 4px;
    color: var(--cc-text-dim);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  blockquote {
    margin: 0;
    padding: 10px 12px;
    border-left: 3px solid var(--cc-cyan);
    border-radius: 8px;
    background: rgb(255 255 255 / 0.06);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .facts {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 6px 14px;
    margin: 0;
  }
  dt {
    color: var(--cc-text-dim);
  }
  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .how {
    margin: 0;
    color: var(--cc-text-dim);
    font-size: 13px;
  }
  .warn {
    color: var(--cc-amber);
  }
  .warn-box,
  .error {
    margin: 0;
    padding: 8px 10px;
    border-radius: 8px;
    background: rgb(245 165 36 / 0.14);
    color: #ffd79a;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    justify-content: flex-end;
  }
  .btn {
    padding: 7px 14px;
    border-radius: 999px;
    border: 1px solid var(--cc-border);
    background: rgb(255 255 255 / 0.07);
    color: inherit;
    font: inherit;
    font-weight: 600;
    cursor: pointer;
  }
  .btn:disabled {
    opacity: 0.45;
    cursor: default;
  }
  .allow {
    border-color: rgb(57 245 160 / 0.6);
    background: rgb(57 245 160 / 0.16);
  }
  .deny {
    border-color: rgb(255 77 109 / 0.5);
  }
</style>
