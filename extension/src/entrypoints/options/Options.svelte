<script lang="ts">
  import { onMount } from 'svelte';

  import ConnStatus from '@/components/ConnStatus.svelte';
  import ModelSettings from '@/components/ModelSettings.svelte';
  import { APP_NAME } from '@/lib/brand';
  import {
    COMPANION_MODE_LABELS,
    COMPANION_MODES,
    type CompanionMode,
    PTT_CHORD_LABELS,
    PTT_CHORDS,
    type PttChord,
  } from '@/lib/prefs';
  import { request } from '@/lib/sw-client';

  let chord: PttChord = $state('hold-right-ctrl');
  let mode: CompanionMode = $state('dock');
  let sites = $state('');
  let devTimeline = $state(false);
  let message: { ok: boolean; text: string } | null = $state(null);
  let grants: [string, { session: boolean; at: number; used?: number }][] = $state([]);

  async function loadGrants(): Promise<void> {
    const reply = await request({ type: 'grants.list' });
    grants = Object.entries(reply.grants).sort(([a], [b]) => a.localeCompare(b));
  }

  async function forget(site: string): Promise<void> {
    await request({ type: 'grants.remove', site });
    await loadGrants();
  }

  async function forgetAll(): Promise<void> {
    await request({ type: 'grants.clear' });
    await loadGrants();
  }

  function day(at: number): string {
    return new Date(at).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }

  onMount(() => {
    void loadGrants();
    void request({ type: 'settings.read' }).then((prefs) => {
      chord = prefs.pttChord;
      mode = prefs.companionMode;
      sites = prefs.disabledSites.join('\n');
      devTimeline = prefs.devTimeline;
    });
  });

  async function save(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const disabledSites = sites.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
    const result = await request({ type: 'settings.set', patch: { pttChord: chord, companionMode: mode, disabledSites, devTimeline } });
    message = result.ok ? { ok: true, text: 'Saved.' } : { ok: false, text: result.error };
    if (result.ok) sites = result.prefs.disabledSites.join('\n');
  }
</script>

<main>
  <h1>{APP_NAME} settings</h1>
  <ConnStatus />
  <form class="cc-card" onsubmit={save}>
    <label>
      Push-to-talk
      <select bind:value={chord}>
        {#each PTT_CHORDS as value (value)}
          <option {value}>{PTT_CHORD_LABELS[value]}</option>
        {/each}
      </select>
    </label>
    <p class="cc-muted">
      Right Ctrl held alone never clashes with page shortcuts. Ctrl+Space is skipped inside code editors, where it
      means autocomplete.
    </p>
    <label>
      Companion orb
      <select bind:value={mode}>
        {#each COMPANION_MODES as value (value)}
          <option {value}>{COMPANION_MODE_LABELS[value]}</option>
        {/each}
      </select>
    </label>
    <p class="cc-muted">
      Docked, the orb rests at the screen edge: drag it anywhere, click it to type. Following, it floats beside your
      cursor and never blocks a click; open the panel from the orb in docked mode.
    </p>
    <label>
      Disabled sites <span class="cc-muted">(one per line, e.g. <code>bank.example.com</code>)</span>
      <textarea rows="5" bind:value={sites} spellcheck="false"></textarea>
    </label>
    <label class="cc-check">
      <input type="checkbox" data-testid="dev-timeline" bind:checked={devTimeline} />
      Developer: show where the time went under each answer (key release or Enter to transcript, first words, audio)
    </label>
    <button type="submit">Save</button>
    {#if message}<p class={message.ok ? 'cc-ok' : 'cc-error'}>{message.text}</p>{/if}
  </form>
  <section class="cc-card" aria-labelledby="grants-title">
    <h2 id="grants-title">Browser tasks you always allow</h2>
    <p class="cc-muted">
      On these sites, a browser task that stays on the site starts without the consent window (it still asks you
      before anything that can't be undone). A task that goes to another site always asks.
    </p>
    {#if grants.length}
      <ul class="cc-grants" data-testid="grants">
        {#each grants as [site, grant] (site)}
          <li>
            <span>
              <strong>{site}</strong>{grant.session ? ' · with your login' : ' · without your login'}
              <span class="cc-muted"> · allowed {day(grant.at)}{grant.used ? `, last used ${day(grant.used)}` : ''}</span>
            </span>
            <button type="button" onclick={() => forget(site)}>Forget</button>
          </li>
        {/each}
      </ul>
      <button type="button" data-testid="grants-clear" onclick={() => void forgetAll()}>Forget all</button>
    {:else}
      <p class="cc-muted" data-testid="grants-empty">None yet.</p>
    {/if}
    <p class="cc-muted">
      A site you haven't used for 90 days is forgotten, and so is every site when you pair with another backend.
    </p>
  </section>
  <ModelSettings />
</main>

<style>
  .cc-grants {
    margin: 0;
    padding: 0;
    list-style: none;
    display: grid;
    gap: 6px;
  }
  .cc-grants li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  h2 {
    margin: 0 0 6px;
    font-size: 15px;
  }

  main {
    max-width: 640px;
    margin: 0 auto;
    padding: 40px 20px;
    display: grid;
    gap: 16px;
  }
  form {
    display: grid;
    gap: 12px;
  }
  .cc-ok {
    color: var(--cc-green);
  }
  .cc-check {
    display: flex;
    gap: 8px;
    align-items: baseline;
  }
  .cc-error {
    color: var(--cc-red);
  }
</style>

