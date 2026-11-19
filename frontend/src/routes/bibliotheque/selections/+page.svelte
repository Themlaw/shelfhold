<script lang="ts">
  // Saved selections (EF-BIB-017): a stored query, evaluated by the server each time it is read, in
  // the scope of this profile. The screen draws what the server answers (the covers and the count
  // are already hers: nothing hidden is in them) and writes a query of the closed grammar only:
  // choices and short words, never an expression. A selection is private to the profile.
  import { onMount } from 'svelte';
  import {
    createSelection,
    deleteSelection,
    listSelections,
    readSelection
  } from '#lib/api/index.ts';
  import type { SelectionDetailOut, SelectionOut } from '#lib/api/types.gen.ts';
  import CoverGrid, { type CoverItem } from '#lib/components/CoverGrid.svelte';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  const STATES = ['unread', 'in_progress', 'read'] as const;

  let items = $state<SelectionOut[]>([]);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);
  let actionProblem = $state<ProblemView | null>(null);
  let busy = $state(false);
  let opened = $state<SelectionDetailOut | null>(null);
  let name = $state('');
  let states = $state<string[]>([]);
  let kinds = $state('');
  let langs = $state('');
  let days = $state('');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const words = (raw: string): string[] =>
    raw
      .split(/[\s,]+/)
      .map((w) => w.trim())
      .filter((w) => w !== '');
  const covers = $derived<CoverItem[]>(
    (opened?.items ?? []).map((w) => ({
      id: w.work_id,
      title: w.title,
      coverHash: w.cover_hash ?? null,
      href: null
    }))
  );

  async function load(): Promise<void> {
    const { data, error } = await listSelections();
    if (error !== undefined || !data) problem = problemView(error);
    else {
      problem = null;
      items = data.items;
    }
    loaded = true;
  }

  async function open(id: string): Promise<void> {
    actionProblem = null;
    const { data, error } = await readSelection({ path: { selection_id: id } });
    if (error !== undefined || !data) {
      actionProblem = problemView(error);
      opened = null;
    } else opened = data;
  }

  async function create(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    actionProblem = null;
    const query: Record<string, unknown> = {};
    if (states.length) query.reading_state = states;
    if (words(kinds).length) query.kind = words(kinds);
    if (words(langs).length) query.lang = words(langs);
    if (days.trim() !== '') query.added_within_days = Number(days);
    try {
      const { error } = await createSelection({ headers: headers(), body: { name, query } });
      if (error !== undefined) actionProblem = problemView(error);
      else {
        name = '';
        states = [];
        kinds = langs = days = '';
        await load();
      }
    } finally {
      busy = false;
    }
  }

  async function remove(id: string): Promise<void> {
    busy = true;
    actionProblem = null;
    try {
      const { error } = await deleteSelection({ headers: headers(), path: { selection_id: id } });
      if (error !== undefined) actionProblem = problemView(error);
      else {
        if (opened?.id === id) opened = null;
        await load();
      }
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    void load();
  });
</script>

<svelte:head>
  <title>{t('selections.title')}</title>
</svelte:head>

<main>
  <h1>{t('selections.title')}</h1>
  <p>{t('selections.intro')}</p>

  {#if !loaded}
    <p>{t('selections.loading')}</p>
  {:else if problem}
    <ProblemCard {problem} />
  {:else if items.length === 0}
    <p>{t('selections.empty')}</p>
  {:else}
    <ul>
      {#each items as item (item.id)}
        <li>
          <button type="button" onclick={() => open(item.id)}>{item.name}</button>
          {#if !item.valid}<span>{t('selections.invalid')}</span>{/if}
          <button type="button" disabled={busy} onclick={() => remove(item.id)}>
            {t('selections.delete', { name: item.name })}
          </button>
        </li>
      {/each}
    </ul>
  {/if}

  {#if actionProblem}<ProblemCard problem={actionProblem} />{/if}

  {#if opened}
    <section aria-label={opened.name}>
      <h2>{opened.name}</h2>
      <p role="status">
        {t('selections.total', { count: opened.total })}{opened.capped ? ' +' : ''}
      </p>
      {#if opened.items.length === 0}
        <p>{t('selections.nothing')}</p>
      {:else}
        <CoverGrid items={covers} label={t('library.covers')} />
      {/if}
    </section>
  {/if}

  <form onsubmit={create}>
    <h2>{t('selections.new')}</h2>
    <label>{t('selections.name')} <input bind:value={name} required maxlength="200" /></label>
    <fieldset>
      <legend>{t('selections.state')}</legend>
      {#each STATES as state (state)}
        <label>
          <input type="checkbox" value={state} bind:group={states} />
          {t(`selections.state.${state}`)}
        </label>
      {/each}
    </fieldset>
    <label>{t('selections.kind')} <input bind:value={kinds} /></label>
    <label>{t('selections.lang')} <input bind:value={langs} /></label>
    <label>
      {t('selections.days')}
      <input type="number" min="1" max="3650" bind:value={days} />
    </label>
    <button type="submit" disabled={busy}>{t('selections.create')}</button>
  </form>
</main>

<style>
  main {
    max-width: 60rem;
    margin: 0 auto;
    padding: 1rem;
  }
  form {
    display: grid;
    gap: 0.5rem;
    margin-top: 1.5rem;
  }
</style>
