<script lang="ts">
  // The Search screen (EF-UX-006, EF-UX-011, EF-USR-014). The admin sees what the sources found,
  // where it looked, and the verbs « Ajouter à ma bibliothèque » and « Demander »; the child sees a
  // title and « Demander », nothing else. The screen decides nothing: the shape of the answer says
  // which one it is, and it makes no call the person's role has no use for (an unhandled call fails).
  import { create as createRequest, search as apiSearch } from '#lib/api/index.ts';
  import type { EditionOut, FamilyItemOut, FamilyOut, FederatedOut, RequestOut, SearchHitOut, SearchedInOut, WorkOut } from '#lib/api/types.gen.ts';
  import AddSheet from '#lib/components/AddSheet.svelte';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import SearchedIn from '#lib/components/SearchedIn.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  let query = $state('');
  let loading = $state(false);
  let problem = $state<ProblemView | null>(null);
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

  function onSubmit(event: SubmitEvent) {
    event.preventDefault();
    void doSearch(false);
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

<main>
  <h1>{t('search.title')}</h1>

  <form role="search" onsubmit={onSubmit}>
    <label>
      {t('search.query_label')}
      <input type="search" class="searchbox" aria-label={t('search.query_label')} bind:value={query} />
    </label>
    <button type="submit">{t('search.submit')}</button>
  </form>

  {#if loading}
    <p role="status">{t('search.searching')}</p>
  {/if}

  {#if problem}
    <ProblemCard problem={problem} level={2} />
  {/if}

  <div data-testid="search-results" aria-busy={loading}>
    {#if isFamilyView}
      {#each familyItems as item (item.request_ref)}
        <article data-testid="family-result">
          <p>{item.title}</p>
          {#if !familyStatus[item.request_ref]}
            <button type="button" onclick={() => requestFamily(item)}>{t('search.request')}</button>
          {:else}
            <p role="status">{familyStatus[item.request_ref]}</p>
          {/if}
        </article>
      {/each}
      {#each local as hit (hit.work_id)}
        <div data-testid="local-result">{hit.title}</div>
      {/each}
    {:else if isAdminView}
      {#each groups as group (group.group_id)}
        <section data-testid="search-result">
          <h2>{group.title}</h2>
          <ul>
            {#each group.editions as edition (edition.source_key + edition.remote_id)}
              <li>
                <span>{sourceNameOf(edition.source_key)}</span>
                <span>{formatLabel(edition.format)}</span>
                {#if added.has(editionKey(edition))}
                  <span>{t('search.added')}</span>
                {:else}
                  <button
                    type="button"
                    aria-label={`${t('search.add')} — ${edition.title} (${sourceNameOf(edition.source_key)})`}
                    onclick={(e) => openAdd(edition, e)}
                  >
                    {t('search.add')}
                  </button>
                {/if}
              </li>
            {/each}
          </ul>
        </section>
      {/each}
      <SearchedIn sources={searchedIn} />
    {:else if hits.length > 0}
      {#each hits as hit (hit.work_id)}
        <p>{hit.title}</p>
      {/each}
    {/if}

    {#if status === 'empty' && groups.length === 0 && !isFamilyView}
      <p>{t('search.empty')}</p>
      {#if action === 'request' && !requested}
        <button type="button" onclick={requestFreeText}>{t('search.request')}</button>
      {/if}
      {#if requested}
        <p role="status">{t('search.requested')}</p>
      {/if}
      {#if openUrl}
        <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- an address the server gave, already checked to be http(s) -->
        <a href={openUrl} rel="noopener noreferrer" target="_blank">{t('search.open_kavita')}</a>
      {/if}
    {/if}

    {#if status === 'empty' && isFamilyView && familyItems.length === 0}
      <p>{t('search.empty')}</p>
      {#if !requested}
        <button type="button" onclick={requestFreeText}>{t('search.request')}</button>
      {:else}
        <p role="status">{t('search.requested')}</p>
      {/if}
    {/if}

    {#if status === 'unavailable'}
      <p>{t('search.unavailable')}</p>
    {/if}

    {#if status === 'no_sources'}
      <p>{t('search.no_sources')}</p>
      <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
      <a href="/reglages">{t('search.open_settings')}</a>
    {/if}

    {#if nextCursor}
      <button type="button" onclick={() => doSearch(true)}>{t('search.more')}</button>
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
  main {
    display: grid;
    gap: 1rem;
    padding: 1rem;
  }
  form {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }
  .searchbox {
    min-height: 2.75rem;
  }
  [data-testid='search-result'] {
    margin: 1rem 0;
  }
</style>
