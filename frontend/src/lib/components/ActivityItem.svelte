<script lang="ts">
  // EF-UX-010: one state for the family, the steps for the admin. The screen shows what the server
  // sent and decides nothing: `family_state` and `open_url` are the server's words, the step
  // (`payload.state`) and `koreader_reset` only reach an admin, so a family item simply has none.
  import type { ActivityOut } from '#lib/api/types.gen.ts';
  import OpenInKavita from '#lib/components/OpenInKavita.svelte';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';

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
    <Badge tone={item.family_state === 'available' ? 'success' : 'neutral'} icon={item.family_state === 'available' ? 'check' : 'clock'}>{label}</Badge>
  {/if}
  {#if item.family_state === 'available'}
    <OpenInKavita url={item.open_url} />
  {/if}
  {#if step}
    <span class="step">{t('activity.step', { step })}</span>
  {/if}
  {#if item.koreader_reset}
    <Badge tone="warning">{t('activity.koreader_reset')}</Badge>
  {/if}
</article>

<style>
  .activity-item {
    display: flex;
    gap: var(--space-2) var(--space-3);
    align-items: center;
    flex-wrap: wrap;
    padding: var(--space-3) 0;
  }
  .title {
    font-weight: 600;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .masked {
    font-style: italic;
    font-weight: 400;
    color: var(--color-muted);
  }
  .step {
    font-size: var(--step--1);
    color: var(--color-muted);
  }
</style>
