<script lang="ts">
  // Collections and reading lists (EF-BIB-016). What a reader may see of a list is decided by the
  // server (the count, the order and the name come only from what she may see): this screen draws
  // it, writes a list whose name the server withheld as "Liste sans nom", and guesses nothing of
  // what is hidden. A CBL file is imported by its text; the lines that could not be resolved are
  // listed, never guessed.
  import { onMount } from 'svelte';
  import { createCollection, importCbl, listCollections } from '#lib/api/index.ts';
  import type { CollectionOut, UnresolvedOut } from '#lib/api/types.gen.ts';
  import { client } from '#lib/api/client.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  let items = $state<CollectionOut[]>([]);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);
  let actionProblem = $state<ProblemView | null>(null);
  let isAdmin = $state(false);
  let busy = $state(false);
  let name = $state('');
  let kind = $state<'collection' | 'reading_list'>('reading_list');
  let scope = $state<'shared' | 'profile'>('profile');
  let imported = $state<number | null>(null);
  let unresolved = $state<UnresolvedOut[]>([]);

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  async function load(): Promise<void> {
    const { data, error } = await listCollections();
    if (error !== undefined || !data) {
      problem = problemView(error);
    } else {
      problem = null;
      items = data.items;
    }
    loaded = true;
  }

  async function create(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    actionProblem = null;
    try {
      const { error } = await createCollection({
        headers: headers(),
        body: { name, kind, scope: isAdmin ? scope : 'profile' }
      });
      if (error !== undefined) {
        actionProblem = problemView(error);
      } else {
        name = '';
        await load();
      }
    } finally {
      busy = false;
    }
  }

  async function importFile(event: Event): Promise<void> {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    busy = true;
    actionProblem = null;
    imported = null;
    unresolved = [];
    try {
      const { data, error } = await importCbl({
        headers: headers(),
        body: { cbl: await file.text(), scope: isAdmin ? scope : 'profile' }
      });
      if (error !== undefined || !data) {
        actionProblem = problemView(error);
      } else {
        imported = data.imported;
        unresolved = data.unresolved;
        await load();
      }
    } finally {
      input.value = '';
      busy = false;
    }
  }

  onMount(() => {
    void (async () => {
      const who = await client.get({ url: '/api/v1/me', throwOnError: false });
      isAdmin = !who.error && (who.data as { role?: string } | undefined)?.role === 'admin';
    })();
    void load();
  });
</script>

<svelte:head>
  <title>{t('collections.title')}</title>
</svelte:head>

<main>
  <h1>{t('collections.title')}</h1>
  <p>{t('collections.intro')}</p>

  {#if !loaded}
    <p>{t('collections.loading')}</p>
  {:else if problem}
    <ProblemCard {problem} />
  {:else}
    {#if items.length === 0}
      <p>{t('collections.empty')}</p>
    {:else}
      <ul class="lists" aria-label={t('collections.title')}>
        {#each items as item (item.id)}
          <li>
            <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
            <a href={`/collections/${item.id}`}>{item.name ?? t('collections.unnamed')}</a>
            <span class="meta">
              {t(`collections.kind.${item.kind}`)} · {t(`collections.scope.${item.scope}`)} ·
              {t('collections.count', { count: item.count })}
            </span>
          </li>
        {/each}
      </ul>
    {/if}

    <section aria-labelledby="new-list">
      <h2 id="new-list">{t('collections.new')}</h2>
      <form onsubmit={create}>
        <label>
          {t('collections.name')}
          <input type="text" bind:value={name} required maxlength="200" autocomplete="off" />
        </label>
        <label>
          {t('collections.kind')}
          <select bind:value={kind}>
            <option value="reading_list">{t('collections.kind.reading_list')}</option>
            <option value="collection">{t('collections.kind.collection')}</option>
          </select>
        </label>
        {#if isAdmin}
          <label>
            {t('collections.scopeLabel')}
            <select bind:value={scope}>
              <option value="profile">{t('collections.scope.profile')}</option>
              <option value="shared">{t('collections.scope.shared')}</option>
            </select>
          </label>
        {/if}
        <button type="submit" disabled={busy || name.trim() === ''}>{t('collections.create')}</button>
      </form>
      <label class="file">
        {t('collections.import')}
        <input type="file" accept=".cbl,.xml,text/xml,application/xml" onchange={importFile} disabled={busy} />
      </label>
    </section>

    {#if actionProblem}
      <ProblemCard problem={actionProblem} />
    {/if}
    {#if imported !== null}
      <p role="status">{t('collections.import_done', { count: imported })}</p>
      {#if unresolved.length > 0}
        <p>{t('collections.unresolved_intro')}</p>
        <ul aria-label={t('collections.unresolved')}>
          {#each unresolved as line (line.index)}
            <li>
              {t('collections.unresolved_line', { line: line.index + 1, series: line.series, number: line.number })}
              — {t(`collections.unresolved.${line.reason}`)}
            </li>
          {/each}
        </ul>
      {/if}
    {/if}
  {/if}
</main>

<style>
  main { max-width: 42rem; margin: 0 auto; padding: 1rem; }
  ul { list-style: none; padding: 0; margin: 0 0 1rem; }
  .lists li { display: grid; gap: 0.125rem; padding: 0.5rem 0; border-bottom: 1px solid var(--color-rule); }
  .lists a { display: inline-block; min-height: 44px; line-height: 44px; }
  .meta { font-size: 0.875rem; }
  section { margin: 1.5rem 0; padding: 1rem; border: 1px solid var(--color-rule); }
  form { display: grid; gap: 0.5rem; max-width: 30rem; }
  label { display: grid; gap: 0.25rem; }
  .file { margin-top: 1rem; }
</style>
