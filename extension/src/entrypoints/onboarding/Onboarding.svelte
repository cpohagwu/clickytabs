<script lang="ts">
  import { onMount } from 'svelte';
  import { browser } from 'wxt/browser';

  import ConnStatus from '@/components/ConnStatus.svelte';
  import { APP_NAME } from '@/lib/brand';
  import { DEFAULT_BACKEND_URL } from '@/lib/settings';
  import { request } from '@/lib/sw-client';

  let backendUrl = $state(DEFAULT_BACKEND_URL);
  let token = $state('');
  let hasToken = $state(false);
  let saving = $state(false);
  let saveMessage: { ok: boolean; text: string } | null = $state(null);

  let micPermission: PermissionState | 'unknown' = $state('unknown');
  let micMessage: string | null = $state(null);
  let probeMessage: string | null = $state(null);
  let shortcuts: { name: string; description: string; shortcut: string }[] = $state([]);

  onMount(() => {
    void request({ type: 'settings.read' }).then((prefs) => {
      backendUrl = prefs.backendUrl;
      hasToken = prefs.hasToken;
    });
    void browser.commands.getAll().then((commands) => {
      shortcuts = commands
        .filter((c) => c.name && !c.name.startsWith('_') && !c.name.startsWith('wxt:'))
        .map((c) => ({ name: c.name ?? '', description: c.description ?? '', shortcut: c.shortcut || 'not set' }));
    });
    let status: PermissionStatus | null = null;
    void navigator.permissions.query({ name: 'microphone' as PermissionName }).then((result) => {
      status = result;
      micPermission = result.state;
      result.onchange = () => (micPermission = result.state);
    });
    if (location.hash === '#mic') document.getElementById('mic')?.scrollIntoView();
    return () => {
      if (status) status.onchange = null;
    };
  });

  async function save(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    saving = true;
    saveMessage = null;
    const patch = token.trim() ? { backendUrl, token: token.trim() } : { backendUrl };
    const result = await request({ type: 'settings.set', patch });
    saving = false;
    if (result.ok) {
      hasToken = result.prefs.hasToken;
      token = '';
      saveMessage = { ok: true, text: 'Saved. Connecting…' };
    } else {
      saveMessage = { ok: false, text: result.error };
    }
  }

  async function grantMic(): Promise<void> {
    micMessage = null;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      for (const track of stream.getTracks()) track.stop();
      micMessage = 'Microphone allowed. If Chrome asked, make sure you chose “Allow on every visit”.';
    } catch (error) {
      micMessage = error instanceof DOMException && error.name === 'NotAllowedError'
        ? 'Chrome blocked the microphone. Open the site settings below and set Microphone to Allow.'
        : `Could not open the microphone: ${error instanceof Error ? error.message : String(error)}`;
    }
  }

  async function probe(): Promise<void> {
    probeMessage = 'Testing…';
    const result = await request({ type: 'debug.micProbe' });
    probeMessage = result.ok
      ? 'Background recording works (the grant persists after this tab closes).'
      : `Background recording failed: ${result.error}`;
  }

  function openShortcuts(): void {
    void browser.tabs.create({ url: 'chrome://extensions/shortcuts' });
  }

  function openSiteSettings(): void {
    void browser.tabs.create({
      url: `chrome://settings/content/siteDetails?site=${encodeURIComponent(`chrome-extension://${browser.runtime.id}`)}`,
    });
  }
</script>

<main>
  <header>
    <h1>Welcome to {APP_NAME}</h1>
    <p class="cc-muted">A voice companion that lives in your tab. Three steps and you're talking.</p>
    <ConnStatus />
  </header>

  <section class="cc-card">
    <h2>1 · Pair with your backend</h2>
    <p class="cc-muted">
      Start the backend (<code>uv run clicky serve</code> in <code>backend/</code>), then copy the token from
      <code>uv run clicky token show</code>.
    </p>
    <form onsubmit={save}>
      <label>
        Backend URL
        <input bind:value={backendUrl} spellcheck="false" autocomplete="off" data-testid="backend-url" />
      </label>
      <label>
        Pairing token {#if hasToken}<span class="cc-muted">(saved; paste a new one to replace it)</span>{/if}
        <input
          type="password"
          bind:value={token}
          spellcheck="false"
          autocomplete="off"
          placeholder={hasToken ? '••••••••••••' : 'paste the token'}
          data-testid="token"
        />
      </label>
      <button type="submit" disabled={saving} data-testid="save">Save &amp; connect</button>
      {#if saveMessage}<p class={saveMessage.ok ? 'cc-ok' : 'cc-error'}>{saveMessage.text}</p>{/if}
    </form>
  </section>

  <section class="cc-card" id="mic">
    <h2>2 · Allow the microphone</h2>
    <p class="cc-muted">
      Chrome remembers this once for the extension, so no website ever sees a microphone prompt. Choose
      <strong>Allow on every visit</strong>; “Allow this time” stops working when this tab closes.
    </p>
    <p>Current permission: <span class="cc-pill" data-tone={micPermission === 'granted' ? 'ok' : 'bad'}>{micPermission}</span></p>
    <div class="cc-row">
      <button type="button" onclick={grantMic}>Allow microphone</button>
      <button type="button" class="cc-secondary" onclick={probe}>Test background recording</button>
      <button type="button" class="cc-secondary" onclick={openSiteSettings}>Site settings</button>
    </div>
    {#if micMessage}<p>{micMessage}</p>{/if}
    {#if probeMessage}<p class="cc-muted">{probeMessage}</p>{/if}
  </section>

  <section class="cc-card">
    <h2>3 · Talk</h2>
    <p class="cc-muted">
      Hold <kbd>Right Ctrl</kbd> on any page and speak, or type in the companion's panel. Change the chord in
      Settings.
    </p>
    <ul>
      {#each shortcuts as s (s.name)}
        <li><kbd>{s.shortcut}</kbd> {s.description}</li>
      {/each}
    </ul>
    <button type="button" class="cc-secondary" onclick={openShortcuts}>Change keyboard shortcuts</button>
  </section>
</main>

<style>
  main {
    max-width: 720px;
    margin: 0 auto;
    padding: 48px 20px;
    display: grid;
    gap: 18px;
  }
  form {
    display: grid;
    gap: 12px;
    margin-top: 10px;
  }
  .cc-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .cc-ok {
    color: var(--cc-green);
  }
  .cc-error {
    color: var(--cc-red);
  }
  ul {
    padding-left: 18px;
  }
</style>
