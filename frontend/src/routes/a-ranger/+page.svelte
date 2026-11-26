<script lang="ts">
  // The "À ranger" view (EF-EXP-046): the Editions on no shelf. They are not exported; their
  // copy is kept as received. Read only: choosing a shelf is not part of this screen.
  import { onMount } from 'svelte';
  import { toShelveList } from '#lib/api/index.ts';
  import type { ToShelveItem } from '#lib/api/types.gen.ts';
  import { t } from '#lib/i18n/index.ts';
  import { needsSession } from '#lib/triage.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import type { Column } from '#lib/ui/molecules/TableRow.svelte';
  import DataTable from '#lib/ui/organisms/DataTable.svelte';

  let items = $state<ToShelveItem[]>([]);
  let loaded = $state(false);
  let signedOut = $state(false);
  let failed = $state(false);

  const columns: readonly Column[] = [
    { key: 'title', label: t('toShelve.col.title'), primary: true },
    { key: 'lang', label: t('toShelve.col.lang') },
    { key: 'media', label: t('toShelve.col.media') }
  ];

  onMount(async () => {
    try {
      const { data } = await toShelveList({ throwOnError: true });
      items = data.items;
      loaded = true;
    } catch (error) {
      if (needsSession(error)) signedOut = true;
      else failed = true;
    }
  });
</script>

<svelte:head>
  <title>{t('toShelve.title')}</title>
</svelte:head>

<main class="page">
  <div class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{t('toShelve.title')}</h1>
      <p class="lede">{t('toShelve.explain')}</p>
    </div>
    <div class="page-head__actions">
      <Button href="/a-ranger/meme-oeuvre">{t('toShelve.same_work')}</Button>
    </div>
  </div>

  {#if signedOut}
    <Alert tone="danger" live="assertive" title={t('toRate.signed_out')} />
  {:else if failed}
    <Alert tone="danger" live="assertive" title={t('toShelve.error')} />
  {:else if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('toShelve.loading')}</p>
      <div class="loading">
        {#each { length: 5 }, index (index)}
          <Skeleton />
        {/each}
      </div>
    </div>
  {:else}
    <DataTable
      caption={t('toShelve.caption')}
      captionHidden
      {columns}
      rows={items}
      key={(item) => item.edition_id}
      compact
    >
      {#snippet cell(column, item)}
        {#if column.key === 'title'}
          <span class="long">{item.title}</span>
        {:else if column.key === 'lang'}
          {item.lang}
        {:else}
          {item.media}
        {/if}
      {/snippet}
      {#snippet empty()}
        <EmptyState title={t('toShelve.none')} />
      {/snippet}
    </DataTable>
  {/if}
</main>

<style>
  .loading {
    display: grid;
    gap: var(--space-4);
  }
  .long {
    overflow-wrap: anywhere;
  }
</style>
