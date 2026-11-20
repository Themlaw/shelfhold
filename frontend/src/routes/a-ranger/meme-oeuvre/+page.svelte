<script lang="ts">
  // The « Même œuvre ? » screen (EF-BIB-029): the proposals of duplicate Works — a series that
  // moved sources in Suwayomi first — and the merge plan through the existing API, no new merge
  // rule: the M3 door (`POST /works/merge`) for the Works, the M4 door (`POST /editions/merge`)
  // for the Editions, whose plan consults the tombstones. The screen only shows a plan and sends
  // back the fingerprint of the plan the admin saw. The actions are reserved: a stale session
  // asks for the password in place and runs the step again after.
  import { onMount } from 'svelte';
  import {
    editionsList,
    editionsMerge,
    workSuggestions,
    workSuggestionsScan,
    worksMerge
  } from '#lib/api/index.ts';
  import type {
    EditionLineOut,
    EditionMergeOut,
    SuggestionOut,
    WorkMergeOut
  } from '#lib/api/types.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { isForbidden, needsReauth, needsSession, problemOf } from '#lib/triage.ts';

  const REASONS: Record<string, MessageKey> = {
    suwayomi_migration: 'sameWork.reason.suwayomi_migration',
    same_title: 'sameWork.reason.same_title'
  };

  let suggestions = $state<SuggestionOut[]>([]);
  let editions = $state<EditionLineOut[]>([]);
  let loaded = $state(false);
  let signedOut = $state(false);
  let forbidden = $state(false);
  let failed = $state(false);
  let selected = $state<SuggestionOut | null>(null);
  let keepId = $state('');
  let workPlan = $state<WorkMergeOut | null>(null);
  let editionKeep = $state('');
  let editionDrop = $state('');
  let editionPlan = $state<EditionMergeOut | null>(null);
  let problem = $state<ProblemView | null>(null);
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;
  let busy = $state(false);

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const titleOf = (workId: string): string =>
    editions.find((e) => e.work_id === workId)?.title ?? workId;
  const dropId = $derived(selected ? (selected.a === keepId ? selected.b : selected.a) : '');
  const editionOptions = $derived.by(() => {
    const s = selected;
    return s ? editions.filter((e) => e.work_id === s.a || e.work_id === s.b) : [];
  });
  const editionLabel = (e: EditionLineOut): string =>
    `${titleOf(e.work_id)} · ${e.lang} · ${e.edition_id}`;

  async function load(): Promise<void> {
    try {
      const [s, e] = await Promise.all([
        workSuggestions({ throwOnError: true }),
        editionsList({ query: { limit: 1000 }, throwOnError: true })
      ]);
      suggestions = s.data.items;
      editions = e.data.items;
      loaded = true;
    } catch (error) {
      if (needsSession(error)) signedOut = true;
      else if (isForbidden(error)) forbidden = true;
      else failed = true;
    }
  }

  onMount(load);

  /** Run a step; a stale password asks for it in place and the step runs again after. */
  async function guarded(step: () => Promise<{ error?: unknown }>): Promise<boolean> {
    busy = true;
    try {
      const { error } = await step();
      if (error === undefined) {
        problem = null;
        return true;
      }
      if (needsReauth(error)) {
        retry = async () => {
          await guarded(step);
        };
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else {
        problem = problemView(error);
      }
      return false;
    } finally {
      busy = false;
    }
  }

  async function afterReauth(): Promise<void> {
    const again = retry;
    retry = null;
    reauthMethods = null;
    if (again) await again();
  }

  function cancelReauth(): void {
    retry = null;
    reauthMethods = null;
  }

  async function scan(): Promise<void> {
    await guarded(async () => {
      const done = await workSuggestionsScan({ headers: headers() });
      if (done.error === undefined) await load();
      return done;
    });
  }

  function open(suggestion: SuggestionOut): void {
    selected = suggestion;
    keepId = suggestion.a;
    workPlan = null;
    editionPlan = null;
    const options = editionOptions;
    editionKeep = options.find((e) => e.work_id === suggestion.a)?.edition_id ?? '';
    editionDrop = options.find((e) => e.work_id === suggestion.b)?.edition_id ?? '';
  }

  function back(): void {
    selected = null;
    workPlan = null;
    editionPlan = null;
  }

  async function planWork(): Promise<void> {
    if (!selected) return;
    const keep = keepId;
    const drop = dropId;
    await guarded(async () => {
      const done = await worksMerge({ headers: headers(), body: { keep_id: keep, drop_id: drop } });
      if (done.data) workPlan = done.data;
      return done;
    });
  }

  async function applyWork(): Promise<void> {
    if (!selected || !workPlan) return;
    const keep = keepId;
    const drop = dropId;
    const fingerprint = workPlan.fingerprint;
    await guarded(async () => {
      const done = await worksMerge({
        headers: headers(),
        body: { keep_id: keep, drop_id: drop, confirm: fingerprint }
      });
      if (done.data) workPlan = done.data;
      if (done.error === undefined) await load();
      return done;
    });
  }

  async function planEdition(): Promise<void> {
    await guarded(async () => {
      const done = await editionsMerge({
        headers: headers(),
        body: { keep_id: editionKeep, drop_id: editionDrop }
      });
      if (done.data) editionPlan = done.data;
      return done;
    });
  }

  async function applyEdition(): Promise<void> {
    if (!editionPlan) return;
    const fingerprint = editionPlan.fingerprint;
    await guarded(async () => {
      const done = await editionsMerge({
        headers: headers(),
        body: { keep_id: editionKeep, drop_id: editionDrop, confirm: fingerprint }
      });
      if (done.data) editionPlan = done.data;
      if (done.error === undefined) await load();
      return done;
    });
  }
</script>

<svelte:head>
  <title>{t('sameWork.title')}</title>
</svelte:head>

<main>
  <h1>{t('sameWork.title')}</h1>
  <p class="muted">{t('sameWork.intro')}</p>

  {#if signedOut}
    <p role="alert" class="err">{t('toRate.signed_out')}</p>
  {:else if forbidden}
    <p role="alert" class="err">{t('sameWork.forbidden')}</p>
  {:else if failed}
    <p role="alert" class="err">{t('sameWork.error')}</p>
  {:else}
    <p>
      <button type="button" onclick={scan} disabled={busy}>{t('sameWork.scan')}</button>
    </p>
    {#if loaded && suggestions.length === 0}
      <p>{t('sameWork.none')}</p>
    {:else if suggestions.length > 0 && !selected}
      <ul>
        {#each suggestions as suggestion (suggestion.id)}
          <li>
            <span>
              <strong>{t(REASONS[suggestion.reason] ?? 'sameWork.reason.same_title')}</strong>
              <span class="muted">
                « {titleOf(suggestion.a)} » / « {titleOf(suggestion.b)} »
              </span>
            </span>
            <button type="button" onclick={() => open(suggestion)}>{t('sameWork.open')}</button>
          </li>
        {/each}
      </ul>
    {:else if selected}
      <p>
        <button type="button" onclick={back}>{t('sameWork.back')}</button>
      </p>
      <h2>« {titleOf(selected.a)} » / « {titleOf(selected.b)} »</h2>

      <section>
        <h3>{t('sameWork.plan')}</h3>
        <label>
          {t('sameWork.keep')}
          <select bind:value={keepId}>
            <option value={selected.a}>« {titleOf(selected.a)} »</option>
            <option value={selected.b}>« {titleOf(selected.b)} »</option>
          </select>
        </label>
        <button type="button" onclick={planWork} disabled={busy}>{t('sameWork.open')}</button>
        {#if workPlan}
          <p>{t('sameWork.plan.editions', { count: workPlan.editions.length })}</p>
          {#if workPlan.blockers.length > 0}
            <p role="alert" class="err">
              {t('sameWork.plan.blockers', { reasons: workPlan.blockers.join(', ') })}
            </p>
          {/if}
          <p class="muted">
            {t('sameWork.plan.ages', {
              keep: workPlan.keep_age ?? '—',
              drop: workPlan.drop_age ?? '—',
              merged: workPlan.merged_age ?? '—'
            })}
          </p>
          {#if workPlan.unrated_after}
            <p class="muted">{t('sameWork.plan.unrated')}</p>
          {/if}
          {#if workPlan.series_plan}
            <p class="muted">
              {t('sameWork.plan.series', { count: workPlan.series_plan.moves.length })}
            </p>
          {/if}
          {#if workPlan.applied}
            <p role="status">{t('sameWork.applied')}</p>
          {:else if workPlan.mergeable}
            <button type="button" onclick={applyWork} disabled={busy}>
              {t('sameWork.apply')}
            </button>
          {/if}
        {/if}
      </section>

      <section>
        <h3>{t('sameWork.editions.title')}</h3>
        <label>
          {t('sameWork.editions.keep')}
          <select bind:value={editionKeep}>
            {#each editionOptions as e (e.edition_id)}
              <option value={e.edition_id}>{editionLabel(e)}</option>
            {/each}
          </select>
        </label>
        <label>
          {t('sameWork.editions.drop')}
          <select bind:value={editionDrop}>
            {#each editionOptions as e (e.edition_id)}
              <option value={e.edition_id}>{editionLabel(e)}</option>
            {/each}
          </select>
        </label>
        <button type="button" onclick={planEdition} disabled={busy || !editionKeep || !editionDrop}>
          {t('sameWork.editions.plan')}
        </button>
        {#if editionPlan}
          <p>
            {t('sameWork.editions.moves', {
              units: editionPlan.units.length,
              volumes: editionPlan.volumes.length,
              bindings: editionPlan.bindings.length
            })}
          </p>
          {#if editionPlan.blockers.length > 0}
            <p role="alert" class="err">
              {t('sameWork.plan.blockers', { reasons: editionPlan.blockers.join(', ') })}
            </p>
          {/if}
          {#if editionPlan.tombstoned_units > 0}
            <p role="alert" class="err">
              {t('sameWork.editions.tombstoned_units', { count: editionPlan.tombstoned_units })}
            </p>
          {/if}
          {#if editionPlan.tombstoned_ids > 0}
            <p role="alert" class="err">
              {t('sameWork.editions.tombstoned_ids', { count: editionPlan.tombstoned_ids })}
            </p>
          {/if}
          {#if editionPlan.applied}
            <p role="status">{t('sameWork.editions.applied')}</p>
          {:else if editionPlan.mergeable}
            <button type="button" onclick={applyEdition} disabled={busy}>
              {t('sameWork.editions.apply')}
            </button>
          {/if}
        {/if}
      </section>
    {/if}
  {/if}

  {#if problem}<ProblemCard {problem} level={2} />{/if}
  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
  {/if}
</main>

<style>
  main {
    max-width: 42rem;
    margin: 0 auto;
    padding: 3rem 1rem;
  }
  h1 {
    font-family: var(--font-display);
    font-weight: 400;
    font-style: italic;
    letter-spacing: -0.02em;
  }
  ul {
    list-style: none;
    padding: 0;
    display: grid;
    gap: 0.75rem;
  }
  li {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    padding: 0.75rem 1rem;
    border: 1px solid var(--color-rule);
    border-radius: var(--radius-cover);
    background: var(--color-surface);
    overflow-wrap: anywhere;
  }
  section {
    margin-top: 1.5rem;
    display: grid;
    gap: 0.75rem;
    justify-items: start;
  }
  label {
    display: grid;
    gap: 0.25rem;
  }
  .muted {
    color: var(--color-muted);
  }
  .err {
    color: var(--color-err);
  }
</style>
