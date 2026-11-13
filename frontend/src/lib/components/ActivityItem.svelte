<script lang="ts">
  // EF-UX-010: one state for the family, the steps for the admin. The screen shows what the server
  // sent and decides nothing: `family_state` and `open_url` are the server's words, the step
  // (`payload.state`) and `koreader_reset` only reach an admin, so a family item simply has none.
  import type { ActivityOut } from '#lib/api/types.gen.ts';
  import OpenInKavita from '#lib/components/OpenInKavita.svelte';
  import { t, type MessageKey } from '#lib/i18n/index.ts';

  let { item }: { item: ActivityOut } = $props();

  const STATES: Record<string, MessageKey> = {
    preparing: 'activity.state.preparing',
    available: 'activity.state.available',
    waiting: 'activity.state.waiting'
  };
  const label = $derived(item.family_state ? t(STATES[item.family_state] ?? 'activity.state.waiting') : '');
  const step = $derived(typeof item.payload.state === 'string' ? item.payload.state : '');
</script>

<article class="activity-item">
  {#if item.title_masked}
    <span class="title masked">{t('activity.masked')}</span>
  {:else if item.title}
    <span class="title">{item.title}</span>
  {/if}
  {#if label}
    <span class="state">{label}</span>
  {/if}
  {#if item.family_state === 'available'}
    <OpenInKavita url={item.open_url} />
  {/if}
  {#if step}
    <span class="step">{t('activity.step', { step })}</span>
  {/if}
  {#if item.koreader_reset}
    <span class="koreader-reset">{t('activity.koreader_reset')}</span>
  {/if}
</article>

<style>
  .activity-item {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--color-rule, #eee);
  }
  .title {
    font-weight: 600;
  }
  .state {
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    border: 1px solid var(--color-rule, #ccc);
  }
  .step,
  .koreader-reset {
    font-size: 0.875rem;
    color: var(--color-muted);
  }
</style>
