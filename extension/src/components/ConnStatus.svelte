<script lang="ts">
  import { onMount } from 'svelte';

  import { CONN_LABELS, type ConnInfo } from '@/lib/conn';
  import { watchConn } from '@/lib/sw-client';

  let info = $state<ConnInfo | null>(null);

  onMount(() => watchConn((next) => (info = next)));

  const tone = $derived(
    info?.state === 'connected' ? 'ok' : info && ['unauthorized', 'rejected', 'unpaired'].includes(info.state) ? 'bad' : 'busy',
  );
</script>

<div class="cc-status">
  <span class="cc-pill" data-tone={tone} data-testid="conn-state" data-state={info?.state ?? 'unknown'}>
    {info ? CONN_LABELS[info.state] : 'Starting…'}
  </span>
  {#if info?.detail && info.state !== 'connected'}
    <p class="cc-muted" data-testid="conn-detail">{info.detail}</p>
  {/if}
  {#if info?.state === 'connected' && info.profile}
    <p class="cc-muted">Profile <code>{info.profile}</code></p>
  {/if}
</div>

<style>
  .cc-status p {
    margin: 6px 0 0;
    font-size: 12px;
  }
</style>
