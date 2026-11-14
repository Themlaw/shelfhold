<script lang="ts">
  // The Search screen (EF-UX-006, EF-UX-011, EF-USR-014). The admin sees what the sources found,
  // where it looked, and the verbs « Ajouter à ma bibliothèque » and « Demander »; the child sees a
  // title and « Demander », nothing else. The screen decides nothing: the shape of the answer says
  // which one it is, and it makes no call the person's role has no use for (an unhandled call fails).
  import { onMount } from 'svelte';
  import { create as createRequest, search as apiSearch } from '#lib/api/index.ts';
  import type { EditionOut, FamilyItemOut, FederatedOut, SearchHitOut, SearchedInOut, WorkOut } from '#lib/api/types.gen.ts';
  import AddSheet from '#lib/components/AddSheet.svelte';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import SearchedIn from '#lib/components/SearchedIn.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  let query = $state('');
  let loading = $state(false);
  let problem = $state<ProblemView | null>(null);
  let raw = $state<Record<string, unknown> | null>(null);
  let currentScope = $state<'all' | 'local'>('all');
  let currentCursor = $state<string | null>(null);
  let items = $state<any[]>([]);
  let searchedIn = $state<SearchedInOut[]>([]);
  let local = $state<SearchHitOut[]>([]);
  let state = $state<string>('');
  let action = $state<string | null>(null);
  let nextCursor = $state<string | null>(null);
  let added = $state<Set<string>>(new Set());
  let requested = $state(false);
  let openUrl = $state<string | null>(null);
  let requestStatus = $state<string | null>(null);
  let familyStatus = $state<Record<string, string>>({});
  let openSheet = $state<{ edition: EditionOut; sourceName: string } | null>(null);
  let focusReturn = $state<HTMLElement | null>(null);

  const isAdminView = $derived(raw !== null && 'searched_in' in raw);
  const isFamilyView = $derived(raw !== null && 'local' in raw && 'request_ref' in (raw.items?.[0] ?? {}));

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
      items = [];
      searchedIn = [];
      local = [];
      nextCursor = null;
      currentCursor = null;
      currentScope = 'all';
      state = '';
      action = null;
      problem = null;
      raw = null;
      requested = false;
      requestStatus = null;
      openUrl = null;
      added = new Set();
      familyStatus = {};
    }
    loading = true;
    problem = null;
    let scope: 'all' | 'local' = append ? currentScope : 'all';
    let cursorParam = append ? currentCursor : null;
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
    const data = resp.data as Record<string, unknown>;
    raw = data;
    if ('searched_in' in data) {
      items = append ? [...items, ...(data.items as any[])] : (data.items as any[]);
      searchedIn = data.searched_in as SearchedInOut[];
      state = String(data.state ?? '');
      action = (data.action as string | null) ?? null;
      nextCursor = (data.next_cursor as string | null) ?? null;
    } else if ('local' in data) {
      items = append ? [...items, ...(data.items as any[])] : (data.items as any[]);
      if (!append) local = (data.local as SearchHitOut[]) ?? [];
      state = String(data.state ?? '');
      nextCursor = (data.next_cursor as string | null) ?? null;
    } else {
      items = (data.items as SearchHitOut[]) ?? [];
      state = 'results';
      nextCursor = null;
    }
    currentCursor = nextCursor;
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
    requestStatus = t('search.requested');
    const url = (data as { open_url?: string | null })?.open_url;
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
    const url = (data as { open_url?: string | null })?.open_url;
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

  function onAdded(state: string) {
    if (openSheet && state === 'followed') {
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
      {#each items as item (item.request_ref)}
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
      {#each items as group (group.group_id)}
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
    {:else if items.length > 0}
      {#each items as hit (hit.work_id)}
        <p>{hit.title}</p>
      {/each}
    {/if}

    {#if state === 'empty' && items.length === 0 && !isFamilyView}
      <p>{t('search.empty')}</p>
      {#if action === 'request' && !requested}
        <button type="button" onclick={requestFreeText} disabled={requested}>{t('search.request')}</button>
      {/if}
      {#if requested && requestStatus}
        <p role="status">{requestStatus}</p>
      {/if}
      {#if openUrl}
        <a href={openUrl} rel="noopener noreferrer" target="_blank">{t('search.open_kavita')}</a>
      {/if}
    {/if}

    {#if state === 'empty' && isFamilyView && items.length === 0}
      <p>{t('search.empty')}</p>
      {#if !requested}
        <button type="button" onclick={requestFreeText}>{t('search.request')}</button>
      {:else}
        <p role="status">{requestStatus}</p>
      {/if}
    {/if}

    {#if state === 'unavailable'}
      <p>{t('search.unavailable')}</p>
      <SearchedIn sources={searchedIn} />
    {/if}

    {#if state === 'no_sources'}
      <p>{t('search.no_sources')}</p>
      <a href="/reglages">{t('search.open_settings')}</a>
    {/if}

    {#if nextCursor}
      <button type="button" onclick={() => doSearch(true)}>{t('search.more')}</button>
    {/if}
  </div>

  {#if requested && openUrl && !isFamilyView}
    <a href={openUrl} rel="noopener noreferrer" target="_blank">{t('search.open_kavita')}</a>
  {/if}

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
