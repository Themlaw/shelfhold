<script lang="ts">
  // The library (EF-BIB-020, EF-SRC-039): the covers of what this reader may see, filtered by series
  // and by shelf. The server decides all of it: the list is already her view (a child gets no
  // hidden Work, no hidden series, no count that includes one), the shelf filter exists only for
  // whoever the server lets read the shelves, and the link to a series sheet only for the admin.
  import { onMount } from 'svelte';
  import { listWorks, me as loadMe, shelves as loadShelves } from '#lib/api/index.ts';
  import type { SeriesChoiceOut, ShelfItemOut, WorkItemOut } from '#lib/api/types.gen.ts';
  import CoverGrid, { type CoverItem } from '#lib/components/CoverGrid.svelte';
  import EmptyState from '#lib/components/EmptyState.svelte';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

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
      return;
    }
    problem = null;
    works = append ? [...works, ...data.items] : data.items;
    total = data.total;
    if (data.series) series = data.series;
    facets = false;
  }

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

<main>
  <h1>{t('library.title')}</h1>

  <div class="filters">
    {#if series.length > 0}
      <label>
        {t('library.filter_series')}
        <select bind:value={seriesId} onchange={() => load(false)}>
          <option value="">{t('library.filter_all')}</option>
          {#each series as choice (choice.id)}
            <option value={choice.id}>{choice.title}</option>
          {/each}
        </select>
      </label>
    {/if}
    {#if shelfChoices && shelfChoices.length > 0}
      <label>
        {t('library.filter_shelf')}
        <select bind:value={shelfId} onchange={() => load(false)}>
          <option value="">{t('library.filter_all')}</option>
          {#each shelfChoices as shelf (shelf.id)}
            <option value={shelf.id}>{shelf.name}</option>
          {/each}
        </select>
      </label>
    {/if}
  </div>

  {#if !loaded}
    <p>{t('library.loading')}</p>
  {:else if problem}
    <ProblemCard {problem} />
  {:else if works.length === 0}
    <EmptyState
      title={t('library.empty_title')}
      description={t('library.empty_description')}
      actionLabel={t('library.empty_action')}
      href="/chercher"
    />
  {:else}
    <p class="total" role="status">{t('library.total', { count: total })}</p>
    <CoverGrid {items} label={t('library.covers')} />
    {#if works.length < total}
      <button type="button" onclick={() => load(true)}>{t('library.more')}</button>
    {/if}
  {/if}
</main>

<style>
  main {
    max-width: 60rem;
    margin: 0 auto;
    padding: 1rem;
  }
  .filters {
    display: flex;
    gap: 1rem;
    flex-wrap: wrap;
    margin-bottom: 1rem;
  }
  .total {
    color: var(--color-muted);
  }
</style>
