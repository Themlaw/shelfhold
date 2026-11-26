<script lang="ts">
  // The library (EF-BIB-020, EF-SRC-039): the covers of what this reader may see, filtered by series
  // and by shelf. The server decides all of it: the list is already her view (a child gets no
  // hidden Work, no hidden series, no count that includes one), the shelf filter exists only for
  // whoever the server lets read the shelves, and the link to a series sheet only for the admin.
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import { listWorks, me as loadMe, shelves as loadShelves } from '#lib/api/index.ts';
  import type { SeriesChoiceOut, ShelfItemOut, WorkItemOut } from '#lib/api/types.gen.ts';
  import CoverGrid, { type CoverItem } from '#lib/components/CoverGrid.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import ProblemAlert, { blockKind, type BlockKind } from '#lib/ui/molecules/ProblemAlert.svelte';
  import Shelf from '#lib/ui/organisms/Shelf.svelte';

  const PAGE = 60;

  let works = $state<WorkItemOut[]>([]);
  let total = $state(0);
  let series = $state<SeriesChoiceOut[]>([]);
  let shelfChoices = $state<ShelfItemOut[] | null>(null);
  let seriesId = $state('');
  let shelfId = $state('');
  let isAdmin = $state(false);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);
  let blocked = $state<BlockKind | null>(null);
  let loadingMore = $state(false);
  let facets = true;

  const items = $derived<CoverItem[]>(
    works.map((w) => ({
      id: w.work_id,
      title: w.title,
      coverHash: w.cover_hash ?? null,
      href: isAdmin && w.edition_id ? `/serie/${w.edition_id}` : null
    }))
  );

  async function load(append: boolean): Promise<void> {
    const { data, error } = await listWorks({
      query: {
        limit: PAGE,
        offset: append ? works.length : 0,
        ...(seriesId ? { series_id: seriesId } : {}),
        ...(shelfId ? { shelf_id: shelfId } : {}),
        ...(facets ? { facets: true } : {})
      }
    });
    loaded = true;
    if (error !== undefined || !data) {
      problem = problemView(error);
      blocked = blockKind(error);
      return;
    }
    problem = null;
    blocked = null;
    works = append ? [...works, ...data.items] : data.items;
    total = data.total;
    if (data.series) series = data.series;
    facets = false;
  }

  async function more(): Promise<void> {
    loadingMore = true;
    try {
      await load(true);
    } finally {
      loadingMore = false;
    }
  }

  function clearFilters(): void {
    seriesId = '';
    shelfId = '';
    void load(false);
  }

  const filtered = $derived(seriesId !== '' || shelfId !== '');

  onMount(async () => {
    const [mine, shelves] = await Promise.all([loadMe(), loadShelves()]);
    isAdmin = mine.data?.role === 'admin';
    shelfChoices = shelves.error === undefined && shelves.data ? shelves.data.items : null;
    await load(false);
  });
</script>

<svelte:head>
  <title>{t('library.title')}</title>
</svelte:head>

<main class="page">
  <div class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{t('library.title')}</h1>
      <p class="lede">{t('library.lede')}</p>
    </div>
    <div class="page-head__actions">
      <Button href="{page.url.pathname}/selections" icon="bookmark">{t('library.selections')}</Button>
    </div>
  </div>

  {#if series.length > 0 || (shelfChoices && shelfChoices.length > 0)}
    <div class="filters">
      {#if series.length > 0}
        <div class="filters__item">
          <Field label={t('library.filter_series')}>
            {#snippet children(control)}
              <Select
                {...control}
                value={seriesId}
                onchange={(event) => {
                  seriesId = event.currentTarget.value;
                  void load(false);
                }}
              >
                <option value="">{t('library.filter_all')}</option>
                {#each series as choice (choice.id)}
                  <option value={choice.id}>{choice.title}</option>
                {/each}
              </Select>
            {/snippet}
          </Field>
        </div>
      {/if}
      {#if shelfChoices && shelfChoices.length > 0}
        <div class="filters__item">
          <Field label={t('library.filter_shelf')}>
            {#snippet children(control)}
              <Select
                {...control}
                value={shelfId}
                onchange={(event) => {
                  shelfId = event.currentTarget.value;
                  void load(false);
                }}
              >
                <option value="">{t('library.filter_all')}</option>
                {#each shelfChoices as shelf (shelf.id)}
                  <option value={shelf.id}>{shelf.name}</option>
                {/each}
              </Select>
            {/snippet}
          </Field>
        </div>
      {/if}
      {#if filtered}
        <Button variant="ghost" icon="x" onclick={clearFilters}>{t('library.clear_filters')}</Button>
      {/if}
    </div>
  {/if}

  {#if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('library.loading')}</p>
      <Shelf label={t('library.loading')} busy>
        {#each { length: 12 }, index (index)}
          <div role="listitem"><Skeleton shape="cover" /></div>
        {/each}
      </Shelf>
    </div>
  {:else if problem}
    <ProblemAlert {problem} kind={blocked} onretry={() => load(false)} />
  {:else if works.length === 0 && filtered}
    <EmptyState variant="filtered" title={t('library.filtered_title')}>
      <p>{t('library.filtered_description')}</p>
      {#snippet actions()}<Button onclick={clearFilters}>{t('library.clear_filters')}</Button>{/snippet}
    </EmptyState>
  {:else if works.length === 0}
    <EmptyState title={t('library.empty_title')}>
      <p>{t('library.empty_description')}</p>
      {#snippet actions()}<Button href="/chercher" variant="primary" icon="search">{t('library.empty_action')}</Button>{/snippet}
    </EmptyState>
  {:else}
    <p class="folio total" role="status">{t('library.total', { count: total })}</p>
    <CoverGrid {items} label={t('library.covers')} />
    {#if works.length < total}
      <div class="more">
        <Button loading={loadingMore} onclick={more}>{t('library.more')}</Button>
      </div>
    {/if}
  {/if}
</main>

<style>
  .filters {
    display: flex;
    flex-wrap: wrap;
    align-items: end;
    gap: var(--space-4);
    margin-bottom: var(--space-6);
  }
  .filters__item {
    flex: 1 1 12rem;
    max-width: 20rem;
    min-width: 0;
  }
  .total {
    margin: 0 0 var(--space-4);
  }
  .more {
    display: flex;
    justify-content: center;
    margin-top: var(--space-7);
  }
</style>
