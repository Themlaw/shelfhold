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
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import ProblemAlert, { blockKind, type BlockKind } from '#lib/ui/molecules/ProblemAlert.svelte';

  let items = $state<CollectionOut[]>([]);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);
  let blocked = $state<BlockKind | null>(null);
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
      blocked = blockKind(error);
    } else {
      problem = null;
      blocked = null;
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

<main class="page">
  <div class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{t('collections.title')}</h1>
      <p class="lede">{t('collections.intro')}</p>
    </div>
  </div>

  {#if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('collections.loading')}</p>
      <div class="skeleton-rows"><Skeleton /><Skeleton /><Skeleton /><Skeleton short /></div>
    </div>
  {:else if problem}
    <ProblemAlert {problem} kind={blocked} onretry={load} />
  {:else}
    <section class="section" aria-labelledby="lists-title">
      <!-- the page title already says it: the heading stays for the outline, not for the eye -->
      <h2 id="lists-title" class="visually-hidden">{t('collections.title')}</h2>
      {#if items.length === 0}
        <EmptyState title={t('collections.empty_title')} headingLevel={3}>
          <p>{t('collections.empty_body')}</p>
        </EmptyState>
      {:else}
        <ul class="entries" role="list" aria-label={t('collections.title')}>
          {#each items as item (item.id)}
            <li class="entry">
              <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
              <a class="entry__name standalone" href={`/collections/${item.id}`}>{item.name ?? t('collections.unnamed')}</a>
              <p class="entry__meta">
                <Badge>{t(`collections.kind.${item.kind}`)}</Badge>
                <Badge tone={item.scope === 'shared' ? 'accent' : 'neutral'}>{t(`collections.scope.${item.scope}`)}</Badge>
                <span class="folio">{t('collections.count', { count: item.count })}</span>
              </p>
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <div class="cols section">
      <section class="panel" aria-labelledby="new-list">
        <h2 id="new-list">{t('collections.new')}</h2>
        <form class="form" onsubmit={create}>
          <Field label={t('collections.name')}>
            {#snippet children(control)}
              <Input {...control} type="text" bind:value={name} required maxlength={200} autocomplete="off" />
            {/snippet}
          </Field>
          <Field label={t('collections.kind')}>
            {#snippet children(control)}
              <Select {...control} bind:value={kind}>
                <option value="reading_list">{t('collections.kind.reading_list')}</option>
                <option value="collection">{t('collections.kind.collection')}</option>
              </Select>
            {/snippet}
          </Field>
          {#if isAdmin}
            <Field label={t('collections.scopeLabel')}>
              {#snippet children(control)}
                <Select {...control} bind:value={scope}>
                  <option value="profile">{t('collections.scope.profile')}</option>
                  <option value="shared">{t('collections.scope.shared')}</option>
                </Select>
              {/snippet}
            </Field>
          {/if}
          <div class="btn-row">
            <Button type="submit" variant="primary" icon="plus" loading={busy} disabled={busy || name.trim() === ''}>{t('collections.create')}</Button>
          </div>
        </form>
      </section>

      <section class="panel">
        <h2>{t('collections.import')}</h2>
        <div class="form">
          <Field label={t('collections.import')} hint={t('collections.import_hint')}>
            {#snippet children(control)}
              <input {...control} class="file" type="file" accept=".cbl,.xml,text/xml,application/xml" onchange={importFile} disabled={busy} />
            {/snippet}
          </Field>
        </div>
      </section>
    </div>

    {#if actionProblem}
      <div class="section"><ProblemAlert problem={actionProblem} /></div>
    {/if}
    {#if imported !== null}
      <div class="section">
        <Alert tone="success" title={t('collections.import_done', { count: imported })} live="polite" />
        {#if unresolved.length > 0}
          <Alert tone="warning" title={t('collections.unresolved')}>
            <p>{t('collections.unresolved_intro')}</p>
            <ul class="unresolved" aria-label={t('collections.unresolved')}>
              {#each unresolved as line (line.index)}
                <li>
                  {t('collections.unresolved_line', { line: line.index + 1, series: line.series, number: line.number })}
                  — {t(`collections.unresolved.${line.reason}`)}
                </li>
              {/each}
            </ul>
          </Alert>
        {/if}
      </div>
    {/if}
  {/if}
</main>

<style>
  .skeleton-rows {
    display: grid;
    gap: var(--space-3);
    max-width: 28rem;
  }
  .entry {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-1) var(--space-5);
    padding: var(--space-2) 0;
  }
  .entry__name {
    display: inline-flex;
    align-items: center;
    min-height: var(--target);
    font: 600 var(--step-1) / 1.25 var(--font-text);
    overflow-wrap: anywhere;
  }
  .entry__meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
  }
  .panel h2 {
    margin-top: 0;
  }
  .form {
    display: grid;
    gap: var(--space-5);
  }
  .form :global(.field + .field) {
    margin-top: 0;
  }
  .file {
    max-width: 100%;
    min-height: var(--target);
    font: var(--step-0) / var(--leading-ui) var(--font-ui);
    color: var(--color-ink);
  }
  .file::file-selector-button {
    min-height: var(--target);
    margin-right: var(--space-3);
    padding: 0 var(--space-4);
    font: 500 var(--step-0) / 1 var(--font-ui);
    color: var(--color-ink);
    background: var(--color-surface);
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }
  .unresolved {
    margin: var(--space-2) 0 0;
    padding-left: var(--space-5);
    overflow-wrap: anywhere;
  }
</style>
