<script lang="ts">
  // The library (EF-BIB-020, EF-SRC-039): the volumes of what this reader may see, as a grid or a
  // list, filtered by series and by shelf, in the order she picks, a page at a time. The server
  // decides all of it: the list is already her view (a child gets no hidden Work, no hidden
  // series, no count that includes one), the shelf filter exists only for whoever the server lets
  // read the shelves, and the link to a series sheet only for the admin (the sheet is the admin's
  // door: the list gives no other address to open a Work). The view (filters, order, layout,
  // page) is in the address, so sharing it and going back work.
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount, untrack } from 'svelte';
  import { SvelteURLSearchParams } from 'svelte/reactivity';
  import { listWorks, me as loadMe, shelves as loadShelves } from '#lib/api/index.ts';
  import type { SeriesChoiceOut, ShelfItemOut, WorkItemOut } from '#lib/api/types.gen.ts';
  import { formatNumber, t, type MessageKey } from '#lib/i18n/index.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { variantProperty } from '#lib/themes/variants.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import { coverKind } from '#lib/ui/atoms/cover.ts';
  import Cover from '#lib/ui/atoms/Cover.svelte';
  import Icon from '#lib/ui/atoms/Icon.svelte';
  import Segmented from '#lib/ui/atoms/Segmented.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import Pagination from '#lib/ui/molecules/Pagination.svelte';
  import ProblemAlert, { blockKind, type BlockKind } from '#lib/ui/molecules/ProblemAlert.svelte';
  import SeriesCard from '#lib/ui/molecules/SeriesCard.svelte';
  import Shelf from '#lib/ui/organisms/Shelf.svelte';

  const PAGE = 24;
  type Sort = 'title' | 'recent';
  type Layout = 'grid' | 'list';
  type View = { series: string; shelf: string; sort: Sort; layout: Layout | null; page: number };

  /** The view the address describes: `series`, `etagere`, `tri` (`recent`), `vue` (`liste`), `page`. */
  function parse(params: Pick<URLSearchParams, 'get'>): View {
    const n = Number.parseInt(params.get('page') ?? '', 10);
    const vue = params.get('vue');
    return {
      series: params.get('series') ?? '',
      shelf: params.get('etagere') ?? '',
      sort: params.get('tri') === 'recent' ? 'recent' : 'title',
      layout: vue === 'liste' ? 'list' : vue === 'grille' ? 'grid' : null,
      page: Number.isFinite(n) && n > 1 ? n : 1
    };
  }

  const same = (a: View, b: View): boolean =>
    a.series === b.series && a.shelf === b.shelf && a.sort === b.sort && a.layout === b.layout && a.page === b.page;

  let view = $state<View>(parse(page.url.searchParams));
  let works = $state<WorkItemOut[]>([]);
  let total = $state(0);
  let series = $state<SeriesChoiceOut[]>([]);
  let shelfChoices = $state<ShelfItemOut[] | null>(null);
  let isAdmin = $state(false);
  let loaded = $state(false);
  let ready = false;
  let problem = $state<ProblemView | null>(null);
  let blocked = $state<BlockKind | null>(null);
  let facets = true;
  let themeLayout = $state<Layout>('grid');
  let ticket = 0;

  const layout = $derived<Layout>(view.layout ?? themeLayout);
  const pages = $derived(Math.max(1, Math.ceil(total / PAGE)));
  const filtered = $derived(view.series !== '' || view.shelf !== '');
  const from = $derived((view.page - 1) * PAGE + 1);
  const to = $derived((view.page - 1) * PAGE + works.length);
  const seriesName = $derived(series.find((s) => s.id === view.series)?.title ?? '');
  const shelfName = $derived(shelfChoices?.find((s) => s.id === view.shelf)?.name ?? '');
  const sortOptions = $derived([
    { value: 'title', label: t('library.sort_title') },
    { value: 'recent', label: t('library.sort_recent') }
  ]);
  const layoutOptions = $derived([
    { value: 'grid', label: t('library.view_grid'), icon: 'grid' as const },
    { value: 'list', label: t('library.view_list'), icon: 'list' as const }
  ]);

  const thumb = (hash: string | null | undefined, size: 160 | 320): string | null =>
    hash && /^[0-9a-f]{64}$/.test(hash) ? `/api/v1/thumbnails/${hash}/${size}` : null;
  const href = (work: WorkItemOut): string | null => (isAdmin && work.edition_id ? `/serie/${work.edition_id}` : null);
  const kindLabel = (work: WorkItemOut): string => t(`library.kind_${coverKind(work.kind)}` as MessageKey);

  function address(v: View): string {
    const params = new SvelteURLSearchParams();
    if (v.series) params.set('series', v.series);
    if (v.shelf) params.set('etagere', v.shelf);
    if (v.sort !== 'title') params.set('tri', v.sort);
    if (v.layout) params.set('vue', v.layout === 'list' ? 'liste' : 'grille');
    if (v.page > 1) params.set('page', String(v.page));
    const query = params.toString();
    return `${page.url.pathname}${query ? `?${query}` : ''}`;
  }

  async function load(): Promise<void> {
    const mine = ++ticket;
    const { data, error } = await listWorks({
      query: {
        limit: PAGE,
        offset: (view.page - 1) * PAGE,
        sort: view.sort,
        ...(view.series ? { series_id: view.series } : {}),
        ...(view.shelf ? { shelf_id: view.shelf } : {}),
        ...(facets ? { facets: true } : {})
      }
    });
    if (mine !== ticket) return; // a newer view was asked for meanwhile
    loaded = true;
    if (error !== undefined || !data) {
      problem = problemView(error);
      blocked = blockKind(error);
      return;
    }
    problem = null;
    blocked = null;
    works = data.items;
    total = data.total;
    if (data.series) series = data.series;
    facets = false;
  }

  /** Change the view: a new filter, order or layout starts again at the first page. */
  function change(next: Partial<View>, keepPage = false): void {
    view = { ...view, ...(keepPage || 'page' in next ? {} : { page: 1 }), ...next };
    // eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin
    void goto(address(view), { reset: next.page !== undefined, replace: next.layout !== undefined });
    if (next.layout === undefined || next.page !== undefined) void load();
  }

  function clearFilters(): void {
    change({ series: '', shelf: '' });
  }

  // back and forward: the address changed under us
  const wanted = $derived(parse(page.url.searchParams));
  $effect(() => {
    const next = wanted;
    untrack(() => {
      if (!ready || same(next, view)) return;
      const reload = next.series !== view.series || next.shelf !== view.shelf || next.sort !== view.sort || next.page !== view.page;
      view = next;
      if (reload) void load();
    });
  });

  onMount(async () => {
    const value = getComputedStyle(document.documentElement).getPropertyValue(variantProperty('cover_layout')).trim();
    themeLayout = value === 'list' ? 'list' : 'grid';
    ready = true;
    const [mine, shelves] = await Promise.all([loadMe(), loadShelves()]);
    isAdmin = mine.data?.role === 'admin';
    shelfChoices = shelves.error === undefined && shelves.data ? shelves.data.items : null;
    await load();
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

  <div class="toolbar" role="group" aria-label={t('library.toolbar')}>
    {#if series.length > 0}
      <div class="toolbar__item">
        <Field label={t('library.filter_series')}>
          {#snippet children(control)}
            <Select {...control} value={view.series} onchange={(event) => change({ series: event.currentTarget.value })}>
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
      <div class="toolbar__item">
        <Field label={t('library.filter_shelf')}>
          {#snippet children(control)}
            <Select {...control} value={view.shelf} onchange={(event) => change({ shelf: event.currentTarget.value })}>
              <option value="">{t('library.filter_all')}</option>
              {#each shelfChoices as shelf (shelf.id)}
                <option value={shelf.id}>{shelf.name}</option>
              {/each}
            </Select>
          {/snippet}
        </Field>
      </div>
    {/if}
    <div class="toolbar__item">
      <Field label={t('library.sort')}>
        {#snippet children(control)}
          <Select {...control} value={view.sort} options={sortOptions} onchange={(event) => change({ sort: event.currentTarget.value === 'recent' ? 'recent' : 'title' })} />
        {/snippet}
      </Field>
    </div>
    <div class="toolbar__view">
      <Segmented
        name="library-layout"
        legend={t('library.view')}
        hideLegend
        options={layoutOptions}
        value={layout}
        onchange={(value) => change({ layout: value === 'list' ? 'list' : 'grid' })}
      />
    </div>
  </div>

  {#if filtered}
    <ul class="chips" aria-label={t('library.active_filters')}>
      {#if view.series}
        <li>
          <button type="button" class="chip" onclick={() => change({ series: '' })}>
            <span class="chip__text">{t('library.filter_series')} : {seriesName || view.series}</span>
            <Icon name="x" size="sm" />
            <span class="visually-hidden">{t('library.remove_filter')}</span>
          </button>
        </li>
      {/if}
      {#if view.shelf}
        <li>
          <button type="button" class="chip" onclick={() => change({ shelf: '' })}>
            <span class="chip__text">{t('library.filter_shelf')} : {shelfName || view.shelf}</span>
            <Icon name="x" size="sm" />
            <span class="visually-hidden">{t('library.remove_filter')}</span>
          </button>
        </li>
      {/if}
      <li><Button variant="ghost" onclick={clearFilters}>{t('library.clear_filters')}</Button></li>
    </ul>
  {/if}

  {#if !loaded}
    <div aria-busy="true" class="library-shelf">
      <p class="visually-hidden" role="status">{t('library.loading')}</p>
      <Shelf label={t('library.loading')} busy>
        {#each { length: 12 }, index (index)}
          <div role="listitem"><Skeleton shape="cover" /></div>
        {/each}
      </Shelf>
    </div>
  {:else if problem}
    <ProblemAlert {problem} kind={blocked} onretry={() => load()} />
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
    <p class="folio total" role="status">
      {#if pages > 1}
        {t('library.range', { from: formatNumber(from), to: formatNumber(to), total: formatNumber(total) })}
      {:else}
        {t('library.total', { count: total })}
      {/if}
    </p>
    {#if layout === 'grid'}
      <div class="library-shelf">
        <Shelf label={t('library.covers')}>
          {#each works as work (work.work_id)}
            <div role="listitem">
              <SeriesCard
                title={work.title}
                seed={work.work_id}
                author={work.author ?? null}
                src={thumb(work.cover_hash, 320)}
                kind={coverKind(work.kind)}
                href={href(work)}
                headingLevel={2}
              >
                {#snippet meta()}{kindLabel(work)}{/snippet}
              </SeriesCard>
            </div>
          {/each}
        </Shelf>
      </div>
    {:else}
      <ul class="rows" aria-label={t('library.covers')}>
        {#each works as work (work.work_id)}
          <li class="row">
            <Cover
              title={work.title}
              seed={work.work_id}
              author={work.author ?? null}
              src={thumb(work.cover_hash, 160)}
              kind={coverKind(work.kind)}
              size="sm"
            />
            <div class="row__text">
              {#if href(work)}
                <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
                <a class="row__title" href={href(work)} title={work.title}>{work.title}</a>
              {:else}
                <span class="row__title" title={work.title}>{work.title}</span>
              {/if}
              <span class="row__meta">{work.author ?? t('ui.cover.unknown_author')} · {kindLabel(work)}</span>
            </div>
          </li>
        {/each}
      </ul>
    {/if}
    <Pagination page={view.page} {pages} label={t('library.pagination')} onpage={(target) => change({ page: target })} />
  {/if}
</main>

<style>
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: end;
    gap: var(--space-4);
    margin-bottom: var(--space-4);
  }
  .toolbar__item {
    flex: 1 1 12rem;
    max-width: 18rem;
    min-width: 0;
  }
  .toolbar__view {
    margin-left: auto;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin: 0 0 var(--space-6);
    padding: 0;
    list-style: none;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    min-height: var(--target);
    padding: 0 var(--space-3);
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-radius: var(--radius-sm);
    background: var(--color-surface);
    color: var(--color-ink);
    font: var(--step--1) / 1 var(--font-ui);
    cursor: pointer;
  }
  .chip:hover {
    border-color: var(--color-ink);
  }
  .chip:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }
  .chip__text {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-inline-size: 16rem;
  }
  .total {
    margin: 0 0 var(--space-4);
  }
  /* two volumes to a row on a phone (about 155 px each), not three narrow ones */
  .library-shelf {
    --cover-min: 8.5rem;
  }
  @media (max-width: 40rem) {
    .library-shelf {
      --cover-min: 9.5rem;
    }
  }
  .rows {
    display: grid;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .row {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    min-height: var(--target);
    padding: var(--space-3) 0;
    border-top: var(--rule-hair) solid var(--color-rule);
  }
  .row__text {
    display: grid;
    gap: var(--space-1);
    min-width: 0;
  }
  .row__title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--color-ink);
    font: 600 var(--step-1) / 1.25 var(--font-text);
    text-decoration: none;
  }
  a.row__title {
    display: inline-flex;
    align-items: center;
    min-height: var(--target);
  }
  a.row__title:hover {
    text-decoration: underline;
  }
  a.row__title:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }
  .row__meta {
    color: var(--color-muted);
    font: italic var(--step--1) / 1.3 var(--font-text);
  }
</style>
