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
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Checkbox from '#lib/ui/atoms/Checkbox.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import ProblemAlert, { blockKind, type BlockKind } from '#lib/ui/molecules/ProblemAlert.svelte';
  import Shelf from '#lib/ui/organisms/Shelf.svelte';

  const STATES = ['unread', 'in_progress', 'read'] as const;

  let items = $state<SelectionOut[]>([]);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);
  let blocked = $state<BlockKind | null>(null);
  let opening = $state(false);
  let actionProblem = $state<ProblemView | null>(null);
  let busy = $state(false);
  let opened = $state<SelectionDetailOut | null>(null);
  let name = $state('');
  let states = $state<string[]>([]);
  let kinds = $state('');
  let langs = $state('');
  let days = $state<string | number | null>('');

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

  async function open(id: string): Promise<void> {
    actionProblem = null;
    opening = true;
    try {
      const { data, error } = await readSelection({ path: { selection_id: id } });
      if (error !== undefined || !data) {
        actionProblem = problemView(error);
        opened = null;
      } else opened = data;
    } finally {
      opening = false;
    }
  }

  function toggleState(state: string, on: boolean): void {
    states = on ? [...states, state] : states.filter((s) => s !== state);
  }

  async function create(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    actionProblem = null;
    const query: Record<string, unknown> = {};
    if (states.length) query.reading_state = states;
    if (words(kinds).length) query.kind = words(kinds);
    if (words(langs).length) query.lang = words(langs);
    if (String(days ?? '').trim() !== '') query.added_within_days = Number(days);
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

<main class="page">
  <div class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{t('selections.title')}</h1>
      <p class="lede">{t('selections.intro')}</p>
    </div>
    <div class="page-head__actions">
      <Button href="/bibliotheque" variant="ghost" icon="left">{t('selections.back')}</Button>
    </div>
  </div>

  <section class="section" aria-labelledby="mine-title">
    <div class="section-head">
      <h2 id="mine-title">{t('selections.mine')}</h2>
      {#if loaded && !problem && items.length > 0}<span class="folio">{items.length}</span>{/if}
    </div>
    {#if !loaded}
      <div aria-busy="true">
        <p class="visually-hidden" role="status">{t('selections.loading')}</p>
        <div class="skeleton-rows"><Skeleton /><Skeleton /><Skeleton short /></div>
      </div>
    {:else if problem}
      <ProblemAlert {problem} kind={blocked} onretry={load} />
    {:else if items.length === 0}
      <EmptyState title={t('selections.empty')} headingLevel={3} art={false} />
    {:else}
      <ul class="entries" role="list">
        {#each items as item (item.id)}
          <li class="entry">
            <div class="entry__name">
              <Button variant="ghost" aria-current={opened?.id === item.id ? 'true' : undefined} onclick={() => open(item.id)}>{item.name}</Button>
              {#if !item.valid}<Badge tone="warning">{t('selections.invalid')}</Badge>{/if}
            </div>
            <Button variant="danger-ghost" size="sm" icon="trash" disabled={busy} onclick={() => remove(item.id)}>
              {t('selections.delete', { name: item.name })}
            </Button>
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  {#if actionProblem}
    <div class="section"><ProblemAlert problem={actionProblem} /></div>
  {/if}

  {#if opening}
    <div class="section" aria-busy="true">
      <Shelf label={t('selections.loading')} busy>
        {#each { length: 6 }, index (index)}
          <div role="listitem"><Skeleton shape="cover" /></div>
        {/each}
      </Shelf>
    </div>
  {:else if opened}
    <section class="section" aria-labelledby="opened-title">
      <div class="section-head">
        <h2 id="opened-title">{opened.name}</h2>
        <p class="folio" role="status">
          {t('selections.total', { count: opened.total })}{opened.capped ? ' +' : ''}
        </p>
      </div>
      {#if opened.items.length === 0}
        <EmptyState variant="filtered" title={t('selections.nothing')} headingLevel={3} />
      {:else}
        <CoverGrid items={covers} label={t('library.covers')} />
      {/if}
    </section>
  {/if}

  <section class="section" aria-labelledby="new-title">
    <form class="panel form" onsubmit={create}>
      <h2 id="new-title">{t('selections.new')}</h2>
      <Field label={t('selections.name')}>
        {#snippet children(control)}
          <Input {...control} bind:value={name} required maxlength={200} />
        {/snippet}
      </Field>
      <fieldset class="states">
        <legend>{t('selections.state')}</legend>
        {#each STATES as state (state)}
          <Checkbox
            label={t(`selections.state.${state}`)}
            checked={states.includes(state)}
            onchange={(event) => toggleState(state, event.currentTarget.checked)}
          />
        {/each}
      </fieldset>
      <Field label={t('selections.kind')}>
        {#snippet children(control)}
          <Input {...control} bind:value={kinds} />
        {/snippet}
      </Field>
      <Field label={t('selections.lang')}>
        {#snippet children(control)}
          <Input {...control} bind:value={langs} />
        {/snippet}
      </Field>
      <Field label={t('selections.days')}>
        {#snippet children(control)}
          <Input {...control} type="number" min={1} max={3650} width="short" bind:value={days} />
        {/snippet}
      </Field>
      <div class="btn-row">
        <Button type="submit" variant="primary" loading={busy}>{t('selections.create')}</Button>
      </div>
    </form>
  </section>
</main>

<style>
  .skeleton-rows {
    display: grid;
    gap: var(--space-3);
    max-width: 24rem;
  }
  .entry {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2) var(--space-4);
    padding: var(--space-2) 0;
  }
  .entry__name {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2) var(--space-3);
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .entry__name :global(a),
  .entry__name :global(button) {
    text-align: left;
  }
  .form {
    display: grid;
    gap: var(--space-5);
    max-width: 36rem;
  }
  .form h2 {
    margin: 0;
  }
  .form :global(.field + .field) {
    margin-top: 0;
  }
  .states {
    display: grid;
    gap: var(--space-2);
    margin: 0;
    padding: 0;
    border: 0;
    min-width: 0;
  }
  .states legend {
    padding: 0;
    margin-bottom: var(--space-2);
    font-weight: 700;
  }
</style>
