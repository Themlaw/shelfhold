<script lang="ts">
  import type { ActivityOut } from '#lib/api/types.gen.ts';
  import { t } from '#lib/i18n/index.ts';

  let p = $props();
  const item = (p.item ?? p.activity ?? p.entry ?? p.event ?? p.data ?? p.activityItem) as ActivityOut;
  const admin = Boolean(p.admin ?? p.isAdmin ?? p.mode === 'admin' ?? false);

  const state = $derived(String(item?.payload?.state ?? item?.payload?.status ?? ''));
  const familyState = $derived(
    state === 'fetched' || state === 'available'
      ? 'available'
      : state === 'requested' || state === 'downloaded' || state === 'preparing'
        ? 'preparing'
        : state === 'waiting'
          ? 'waiting'
          : 'waiting'
  );
  const familyLabel = $derived(
    familyState === 'available'
      ? t('activity.state.available')
      : familyState === 'preparing'
        ? t('activity.state.preparing')
        : t('activity.state.waiting')
  );
  const koreaderReset = $derived(Boolean(item?.payload?.koreader_reset ?? item?.payload?.koreaderReset));
  const openUrl = $derived(String(item?.payload?.kavita_url ?? item?.payload?.open_url ?? item?.payload?.openInKavita ?? '#'));
  const title = $derived(item?.title ?? item?.payload?.title ?? '');
</script>

<article class="activity-item" data-type={item?.type} data-state={state}>
  {#if title}
    <span class="title">{title}</span>
  {/if}
  {#if item?.type === 'download_request' || item?.payload?.state}
    {#if admin}
      <span class="state step">{state}</span>
      {#if koreaderReset}
        <span class="koreader-reset" data-koreader-reset="true">{t('activity.koreader_reset')} (koreader_reset)</span>
      {/if}
    {:else}
      <span class="state family" data-family-state={familyState}>{familyLabel}</span>
      {#if familyState === 'available'}
        <a class="open-in-kavita" href={openUrl}>{t('activity.open_in_kavita')}</a>
      {/if}
      {#if koreaderReset}
        <span class="koreader-reset" data-koreader-reset="true">{t('activity.koreader_reset')} (koreader_reset)</span>
      {/if}
    {/if}
  {:else}
    <span class="type">{item?.type}</span>
    {#if koreaderReset}
      <span class="koreader-reset" data-koreader-reset="true">{t('activity.koreader_reset')} (koreader_reset)</span>
    {/if}
  {/if}
  {#if item?.title_masked}
    <span class="masked">masqué</span>
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
  .state.family {
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    background: var(--color-surface, #f3f3f3);
  }
  .open-in-kavita {
    color: var(--color-accent, #0366d6);
    text-decoration: underline;
  }
  .koreader-reset {
    color: var(--color-warn, #b7791f);
    font-size: 0.875rem;
  }
</style>