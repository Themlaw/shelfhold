<script lang="ts">
  // The Search screen (EF-UX-006, EF-UX-011, EF-USR-014). The admin sees what the sources found,
  // where it looked, and the verbs « Ajouter à ma bibliothèque » and « Demander »; the child sees a
  // title and « Demander », nothing else. The screen decides nothing: the shape of the answer says
  // which one it is, and it makes no call the person's role has no use for (an unhandled call fails).
  import { create as createRequest, search as apiSearch } from '#lib/api/index.ts';
  import type { EditionOut, FamilyItemOut, FamilyOut, FederatedOut, RequestOut, SearchHitOut, SearchedInOut, WorkOut } from '#lib/api/types.gen.ts';
  import AddSheet from '#lib/components/AddSheet.svelte';
  import SearchedIn from '#lib/components/SearchedIn.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Icon from '#lib/ui/atoms/Icon.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Spinner from '#lib/ui/atoms/Spinner.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import ProblemAlert, { blockKind, type BlockKind } from '#lib/ui/molecules/ProblemAlert.svelte';
  import SearchBar from '#lib/ui/molecules/SearchBar.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

  let query = $state('');
  let loading = $state(false);
  let problem = $state<ProblemView | null>(null);
  let blocked = $state<BlockKind | null>(null);
  let raw = $state<object | null>(null);
  let currentScope = $state<'all' | 'local'>('all');
  let groups = $state<WorkOut[]>([]);
  let familyItems = $state<FamilyItemOut[]>([]);
  let hits = $state<SearchHitOut[]>([]);
  let searchedIn = $state<SearchedInOut[]>([]);
  let local = $state<SearchHitOut[]>([]);
  let status = $state<string>('');
  let action = $state<string | null>(null);
  let nextCursor = $state<string | null>(null);
  let added = $state<Set<string>>(new Set());
  let requested = $state(false);
  let openUrl = $state<string | null>(null);
  let familyStatus = $state<Record<string, string>>({});
  let openSheet = $state<{ edition: EditionOut; sourceName: string } | null>(null);
  let focusReturn = $state<HTMLElement | null>(null);

  const isAdminView = $derived(raw !== null && 'searched_in' in raw);
  const isFamilyView = $derived(raw !== null && 'local' in raw);
  // some sources did not answer while others did: the list is shown, said to be incomplete
  const partial = $derived(status === 'results' && searchedIn.some((source) => source.status === 'error'));

  function sourceNameOf(key: string): string {
    return searchedIn.find((s) => s.source_key === key)?.name ?? key;
  }

  function formatLabel(format: string): string {
    if (format === 'text') return t('format.text');
    if (format === 'images') return t('format.images');
    return format;
  }

  async function doSearch(append: boolean) {
    const q = query.trim();
    if (!q) return;
    if (!append) {
      groups = [];
      familyItems = [];
      hits = [];
      searchedIn = [];
      local = [];
      nextCursor = null;
      currentScope = 'all';
      status = '';
      action = null;
      problem = null;
      raw = null;
      requested = false;
      openUrl = null;
      added = new Set();
      familyStatus = {};
    }
    loading = true;
    problem = null;
    blocked = null;
    let scope: 'all' | 'local' = append ? currentScope : 'all';
    let cursorParam = append ? nextCursor : null;
    let resp = await apiSearch({
      query: {
        q,
        scope,
        ...(cursorParam ? { cursor: cursorParam } : {})
      },
      throwOnError: false
    });
    if (resp.error !== undefined) {
      if (!append && scope === 'all' && resp.error.status === 403) {
        scope = 'local';
        resp = await apiSearch({
          query: { q, scope: 'local' },
          throwOnError: false
        });
      }
      if (resp.error !== undefined) {
        problem = problemView(resp.error);
        blocked = blockKind(resp.error);
        loading = false;
        return;
      }
    }
    currentScope = scope;
    const data = resp.data as FederatedOut | FamilyOut | { items: SearchHitOut[] };
    raw = data;
    if ('searched_in' in data) {
      groups = append ? [...groups, ...data.items] : data.items;
      searchedIn = data.searched_in;
      status = data.state;
      action = data.action;
      nextCursor = data.next_cursor;
    } else if ('local' in data) {
      familyItems = append ? [...familyItems, ...data.items] : data.items;
      if (!append) local = data.local;
      status = data.state;
      nextCursor = data.next_cursor;
    } else {
      hits = data.items;
      status = 'results';
      nextCursor = null;
    }
    loading = false;
  }

  async function requestFreeText() {
    const { data, error } = await createRequest({
      headers: { 'X-CSRF-Token': csrfToken() },
      body: { target_kind: 'free_text', target_value: query.trim() },
      throwOnError: false
    });
    if (error !== undefined) {
      problem = problemView(error);
      return;
    }
    requested = true;
    const url = (data as RequestOut | undefined)?.open_url;
    if (typeof url === 'string' && /^https?:\/\//.test(url)) {
      openUrl = url;
    }
  }

  async function requestFamily(item: FamilyItemOut) {
    const { data, error } = await createRequest({
      headers: { 'X-CSRF-Token': csrfToken() },
      body: { request_ref: item.request_ref },
      throwOnError: false
    });
    if (error !== undefined) {
      problem = problemView(error);
      return;
    }
    familyStatus = { ...familyStatus, [item.request_ref]: t('search.requested') };
    const url = (data as RequestOut | undefined)?.open_url;
    if (typeof url === 'string' && /^https?:\/\//.test(url)) {
      openUrl = url;
    }
  }

  function openAdd(edition: EditionOut, event: Event) {
    const target = event.currentTarget as HTMLElement;
    focusReturn = target;
    openSheet = { edition, sourceName: sourceNameOf(edition.source_key) };
  }

  function closeSheet() {
    openSheet = null;
    focusReturn?.focus();
  }

  function onAdded(outcome: string) {
    if (openSheet && outcome === 'followed') {
      const key = `${openSheet.edition.source_key}:${openSheet.edition.remote_id}`;
      added = new Set([...added, key]);
    }
  }

  function editionKey(edition: EditionOut): string {
    return `${edition.source_key}:${edition.remote_id}`;
  }
</script>

<svelte:head>
  <title>{t('search.title')}</title>
</svelte:head>

<main class="page">
  <PageHead title={t('search.title')} lede={t('search.lede')} />

  <div class="query">
    <div class="query__bar">
      <SearchBar bind:value={query} label={t('search.query_label')} size="lg" onsearch={() => void doSearch(false)} />
    </div>
    <Button variant="primary" size="lg" icon="search" onclick={() => void doSearch(false)}>{t('search.submit')}</Button>
  </div>

  {#if loading}
    <p class="searching" role="status"><Spinner size="sm" /> {t('search.searching')}</p>
  {/if}

  {#if problem}
    <div class="section"><ProblemAlert {problem} kind={blocked} level={2} /></div>
  {/if}

  <div class="results" data-testid="search-results" aria-busy={loading}>
    {#if loading && groups.length === 0 && familyItems.length === 0 && hits.length === 0}
      <div class="skeleton-rows">
        <Skeleton /><Skeleton /><Skeleton short />
      </div>
    {/if}

    {#if partial}
      <Alert tone="warning" title={t('search.partial')}>
        <p>{t('search.partial_body')}</p>
      </Alert>
    {/if}

    {#if isFamilyView}
      {#each familyItems as item (item.request_ref)}
        <article class="entry" data-testid="family-result">
          <p class="entry__title">{item.title}</p>
          {#if !familyStatus[item.request_ref]}
            <Button size="sm" onclick={() => requestFamily(item)}>{t('search.request')}</Button>
          {:else}
            <p class="done" role="status"><Icon name="check" size="sm" /> {familyStatus[item.request_ref]}</p>
          {/if}
        </article>
      {/each}
      {#if local.length > 0}
        <h2 class="local-title">{t('search.local_title')}</h2>
      {/if}
      {#each local as hit (hit.work_id)}
        <div class="entry entry--local" data-testid="local-result">{hit.title}</div>
      {/each}
    {:else if isAdminView}
      {#each groups as group (group.group_id)}
        <section class="group" data-testid="search-result">
          <h2 class="group__title">{group.title}</h2>
          <ul class="entries">
            {#each group.editions as edition (edition.source_key + edition.remote_id)}
              <li class="edition">
                <span class="edition__source">{sourceNameOf(edition.source_key)}</span>
                <span class="edition__format"><Badge>{formatLabel(edition.format)}</Badge></span>
                {#if added.has(editionKey(edition))}
                  <span class="edition__done"><Badge tone="success">{t('search.added')}</Badge></span>
                {:else}
                  <Button
                    size="sm"
                    icon="plus"
                    aria-label={`${t('search.add')} — ${edition.title} (${sourceNameOf(edition.source_key)})`}
                    onclick={(e: Event) => openAdd(edition, e)}
                  >
                    {t('search.add')}
                  </Button>
                {/if}
              </li>
            {/each}
          </ul>
        </section>
      {/each}
      <SearchedIn sources={searchedIn} />
    {:else if hits.length > 0}
      <ul class="entries">
        {#each hits as hit (hit.work_id)}
          <li class="entry">{hit.title}</li>
        {/each}
      </ul>
    {/if}

    {#if status === 'empty' && groups.length === 0 && !isFamilyView}
      <EmptyState variant="filtered" title={t('search.empty')}>
        {#snippet actions()}
          {#if action === 'request' && !requested}
            <Button variant="primary" onclick={requestFreeText}>{t('search.request')}</Button>
          {/if}
          {#if openUrl}
            <Button href={openUrl} rel="noopener noreferrer" target="_blank" iconEnd="external">{t('search.open_kavita')}</Button>
          {/if}
        {/snippet}
      </EmptyState>
      {#if requested}
        <Alert tone="success" title={t('search.requested')} live="polite" />
      {/if}
    {/if}

    {#if status === 'empty' && isFamilyView && familyItems.length === 0}
      <EmptyState variant="filtered" title={t('search.empty')}>
        {#snippet actions()}
          {#if !requested}<Button variant="primary" onclick={requestFreeText}>{t('search.request')}</Button>{/if}
        {/snippet}
      </EmptyState>
      {#if requested}
        <Alert tone="success" title={t('search.requested')} live="polite" />
      {/if}
    {/if}

    {#if status === 'unavailable'}
      <Alert tone="warning" title={t('search.unavailable')} />
    {/if}

    {#if status === 'no_sources'}
      <EmptyState title={t('search.no_sources')}>
        {#snippet actions()}<Button href="/reglages" variant="primary">{t('search.open_settings')}</Button>{/snippet}
      </EmptyState>
    {/if}

    {#if nextCursor}
      <div class="more">
        <Button {loading} onclick={() => doSearch(true)}>{t('search.more')}</Button>
      </div>
    {/if}
  </div>

  {#if openSheet}
    <AddSheet
      edition={openSheet.edition}
      sourceName={openSheet.sourceName}
      onclose={closeSheet}
      onadded={onAdded}
    />
  {/if}
</main>

<style>
  .query {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-3);
    margin-bottom: var(--space-6);
  }
  .query__bar {
    flex: 1 1 18rem;
    min-width: 0;
  }
  .searching {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin: 0 0 var(--space-4);
    color: var(--color-muted);
  }
  .results {
    display: grid;
    gap: var(--space-6);
  }
  .skeleton-rows {
    display: grid;
    gap: var(--space-3);
    max-width: 28rem;
  }
  .group__title {
    margin: 0 0 var(--space-3);
    font-size: var(--step-2);
    overflow-wrap: anywhere;
  }
  .edition {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2) var(--space-4);
    padding: var(--space-3) 0;
  }
  .edition__source {
    flex: 1 1 10rem;
    min-width: 0;
    font-weight: 600;
    overflow-wrap: anywhere;
  }
  .edition > :global(.btn) {
    margin-left: auto;
  }
  .entry {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2) var(--space-4);
    padding: var(--space-3) 0;
    overflow-wrap: anywhere;
  }
  article.entry,
  div.entry {
    border-bottom: var(--rule-hair) solid var(--color-rule);
  }
  .entry__title {
    flex: 1 1 12rem;
    min-width: 0;
    margin: 0;
    font-weight: 600;
  }
  .entry--local {
    color: var(--color-muted);
  }
  .local-title {
    margin: 0;
    font-size: var(--step-1);
  }
  .done {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
    color: var(--color-ok);
    font-weight: 500;
  }
  .more {
    display: flex;
    justify-content: center;
  }
</style>
