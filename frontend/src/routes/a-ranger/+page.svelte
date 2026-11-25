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
  import DataTable from '#lib/ui/organisms/DataTable.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

  let items = $state<ToShelveItem[]>([]);
  let loaded = $state(false);
  let signedOut = $state(false);
  let failed = $state(false);

  const COLUMNS = [
    { key: 'title', label: t('toShelve.col_title'), primary: true },
    { key: 'lang', label: t('toShelve.col_lang') },
    { key: 'media', label: t('toShelve.col_media') }
  ] as const;

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
  <PageHead title={t('toShelve.title')} lede={t('toShelve.explain')}>
    {#snippet actions()}
      <Button href="/a-ranger/meme-oeuvre" icon="arrows">{t('toShelve.same_work')}</Button>
    {/snippet}
  </PageHead>

  {#if signedOut}
    <Alert tone="warning" title={t('toRate.signed_out')} live="assertive" />
  {:else if failed}
    <Alert tone="danger" title={t('toShelve.error')} live="assertive" />
  {:else if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('toShelve.loading')}</p>
      <Skeleton /><Skeleton /><Skeleton short />
    </div>
  {:else if items.length === 0}
    <EmptyState title={t('toShelve.none')} />
  {:else}
    <DataTable
      caption={t('toShelve.title')}
      captionHidden
      columns={COLUMNS}
      rows={items.map((item) => ({ id: item.edition_id, title: item.title, lang: item.lang, media: item.media }))}
      key={(row) => String(row.id)}
    />
  {/if}
</main>
