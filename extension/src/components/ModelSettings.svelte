<script lang="ts">
  /**
   * Options: the backend's profile and one provider (model, voice) per capability, switched without a restart.
   * The backend validates every change, health-checks the new provider and keeps the old chain as its fallbacks.
   */
  import { onMount } from 'svelte';

  import type { ConfigChange } from '@/lib/messages';
  import type { CapabilityConfig, ConfigState, ProviderChoiceWire, ProviderOption } from '@/lib/protocol.gen';
  import { request } from '@/lib/sw-client';

  const LABELS: Record<string, string> = {
    brain: 'Brain: answers and pointing',
    codegen: 'Scene code',
    agent_llm: 'Browser-task agent',
    stt: 'Speech to text',
    tts: 'Voice',
    summarizer: 'Conversation summaries',
  };
  const HIDDEN = new Set(['embeddings']);            // no feature uses embeddings yet

  interface Draft {
    provider: string;
    model: string;
    voice: string;
  }

  let config = $state<ConfigState | null>(null);
  let drafts: Record<string, Draft> = $state({});
  let error = $state('');
  let note = $state('');
  let busy = $state('');

  const shown = $derived(config ? (config.capabilities ?? []).filter((c) => !HIDDEN.has(c.capability)) : []);

  function draftsFor(next: ConfigState): Record<string, Draft> {
    const result: Record<string, Draft> = {};
    for (const cap of next.capabilities ?? []) {
      const voice = cap.voice_key ? cap.choice?.options?.[cap.voice_key] : undefined;
      result[cap.capability] = {
        provider: cap.choice?.provider ?? cap.status.provider,
        model: cap.choice?.model ?? cap.status.model ?? '',
        voice: typeof voice === 'string' ? voice : '',
      };
    }
    return result;
  }

  function accept(reply: ConfigState | { ok: false; error: string }): boolean {
    if ('ok' in reply) {
      error = reply.error;
      return false;
    }
    config = reply;
    drafts = draftsFor(reply);
    error = reply.error ?? '';
    return !reply.error;
  }

  async function load(): Promise<void> {
    accept(await request({ type: 'config.get' }));
  }

  async function change(next: ConfigChange, what: string): Promise<void> {
    busy = what;
    note = '';
    error = '';
    try {
      if (accept(await request({ type: 'config.set', change: next }))) note = 'Saved. The next question uses it.';
    } finally {
      busy = '';
    }
  }

  function option(cap: CapabilityConfig, name: string): ProviderOption | undefined {
    return (cap.providers ?? []).find((p) => p.name === name);
  }

  function apply(cap: CapabilityConfig): void {
    const draft = drafts[cap.capability];
    if (!draft) return;
    const choice: ProviderChoiceWire = { provider: draft.provider };
    if (draft.model.trim() && (option(cap, draft.provider)?.needs_model ?? true)) choice.model = draft.model.trim();
    // The voice list belongs to the active provider: it only carries over when the provider stays the same.
    if (cap.voice_key && draft.voice && draft.provider === cap.status.provider) {
      choice.options = { [cap.voice_key]: draft.voice };
    }
    void change({ capability: cap.capability as ConfigChange['capability'], choice }, cap.capability);
  }

  function providerText(p: ProviderOption): string {
    const kind = p.kind === 'paid' ? 'paid API' : p.kind === 'open' ? `open, ${p.source}` : 'test';
    return `${p.name} (${kind})${p.available ? '' : ` - ${p.reason}`}`;
  }

  onMount(() => void load());
</script>

<section class="cc-card" aria-labelledby="models-title" data-testid="models">
  <h2 id="models-title">Models and voices</h2>
  {#if !config}
    <p class="cc-muted">{error || 'Asking the backend...'}</p>
  {:else}
    <label>
      Profile
      <select
        data-testid="profile-select"
        value={config.profile}
        disabled={config.profile_pinned || busy !== ''}
        onchange={(e) => void change({ profile: e.currentTarget.value as ConfigChange['profile'] }, 'profile')}
      >
        {#each config.profiles ?? [] as profile (profile)}
          <option value={profile}>{profile}</option>
        {/each}
      </select>
    </label>
    <p class="cc-muted">
      {#if config.profile_pinned}
        The backend was started with a fixed profile (<code>CLICKY_PROFILE</code> or <code>--profile</code>).
      {:else}
        local keeps everything on this machine; cloud uses the paid APIs in <code>backend/.env</code>; hybrid mixes
        them. Each choice below applies to the {config.profile} profile.
      {/if}
    </p>
    <ul class="cc-caps">
      {#each shown as cap (cap.capability)}
        {@const draft = drafts[cap.capability]}
        <li data-testid={`cap-${cap.capability}`}>
          <div class="cc-cap-head">
            <strong>{LABELS[cap.capability] ?? cap.capability}</strong>
            <span class={cap.status.ok ? 'cc-ok' : 'cc-error'} data-testid={`status-${cap.capability}`}>
              {cap.status.provider}{cap.status.model ? `: ${cap.status.model}` : ''}
              {cap.status.ok ? '' : ` - ${cap.status.reason}`}
            </span>
          </div>
          {#if !cap.status.ok && cap.status.hint}<p class="cc-muted">{cap.status.hint}</p>{/if}
          {#if (cap.status.chain ?? []).length > 1}
            <p class="cc-muted">Falls back to {(cap.status.chain ?? []).slice(1).join(', ')}.</p>
          {/if}
          {#if draft}
            <div class="cc-cap-edit">
              <select aria-label="Provider" data-testid={`provider-${cap.capability}`} bind:value={draft.provider}>
                {#each cap.providers ?? [] as p (p.name)}
                  <option value={p.name} disabled={!p.available}>{providerText(p)}</option>
                {/each}
              </select>
              {#if option(cap, draft.provider)?.needs_model ?? true}
                <input
                  aria-label="Model"
                  data-testid={`model-${cap.capability}`}
                  list={`models-${cap.capability}`}
                  placeholder={option(cap, draft.provider)?.default_model ?? 'model'}
                  bind:value={draft.model}
                  spellcheck="false"
                />
                <datalist id={`models-${cap.capability}`}>
                  {#each option(cap, draft.provider)?.models ?? [] as model (model)}<option value={model}></option>{/each}
                </datalist>
              {/if}
              {#if (cap.voices ?? []).length && draft.provider === cap.status.provider}
                <select aria-label="Voice" data-testid={`voice-${cap.capability}`} bind:value={draft.voice}>
                  <option value="">Default voice</option>
                  {#each cap.voices ?? [] as voice (voice.id)}<option value={voice.id}>{voice.name}</option>{/each}
                </select>
              {/if}
              <button type="button" data-testid={`apply-${cap.capability}`} disabled={busy !== ''}
                onclick={() => apply(cap)}>{busy === cap.capability ? 'Checking...' : 'Use'}</button>
              {#if cap.choice}
                <button type="button" data-testid={`reset-${cap.capability}`} disabled={busy !== ''}
                  onclick={() => void change({ capability: cap.capability as ConfigChange['capability'], reset: true },
                    cap.capability)}>Default</button>
              {/if}
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
  {#if error}<p class="cc-error" data-testid="config-error">{error}</p>{/if}
  {#if note}<p class="cc-ok" data-testid="config-note">{note}</p>{/if}
  <p class="cc-muted">
    API keys, hosts and other options stay in <code>backend/.env</code> and <code>backend/clicky.toml</code>; choices
    made here are saved by the backend and win over <code>clicky.toml</code>.
  </p>
</section>

<style>
  h2 {
    margin: 0 0 6px;
    font-size: 15px;
  }
  section {
    display: grid;
    gap: 10px;
  }
  .cc-caps {
    margin: 0;
    padding: 0;
    list-style: none;
    display: grid;
    gap: 14px;
  }
  .cc-cap-head {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 4px 12px;
  }
  .cc-cap-edit {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 6px;
  }
  .cc-cap-edit select,
  .cc-cap-edit input {
    flex: 1 1 180px;
    min-width: 0;
  }
  p {
    margin: 0;
  }
  .cc-ok {
    color: var(--cc-green);
  }
  .cc-error {
    color: var(--cc-red);
  }
</style>
