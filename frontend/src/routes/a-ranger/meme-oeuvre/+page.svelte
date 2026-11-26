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
  import ProblemAlert from '#lib/ui/molecules/ProblemAlert.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { Reauth } from '#lib/guard.svelte.ts';
  import { isForbidden, needsSession } from '#lib/triage.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import ConfirmDialog from '#lib/ui/molecules/ConfirmDialog.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

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
  const reauth = new Reauth();
  let busy = $state(false);
  let confirming = $state<'works' | 'editions' | null>(null);

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
      if (!reauth.askAgain(error, async () => { await guarded(step); })) problem = problemView(error);
      return false;
    } finally {
      busy = false;
    }
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

<main class="page">
  <PageHead
    title={t('sameWork.title')}
    lede={t('sameWork.intro')}
    crumbs={[
      { label: t('settings.title'), href: '/reglages' },
      { label: t('toShelve.title'), href: '/a-ranger' },
      { label: t('sameWork.title') }
    ]}
  >
    {#snippet actions()}
      {#if !signedOut && !forbidden && !failed}
        <Button icon="refresh" onclick={scan} disabled={busy}>{t('sameWork.scan')}</Button>
      {/if}
    {/snippet}
  </PageHead>

  {#if signedOut}
    <Alert tone="danger" live="assertive" title={t('toRate.signed_out')} />
  {:else if forbidden}
    <Alert tone="danger" live="assertive" title={t('sameWork.forbidden')} />
  {:else if failed}
    <Alert tone="danger" live="assertive" title={t('sameWork.error')} />
  {:else if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('sameWork.loading')}</p>
      <div class="loading">
        {#each { length: 4 }, index (index)}
          <Skeleton />
        {/each}
      </div>
    </div>
  {:else if suggestions.length === 0}
    <EmptyState title={t('sameWork.none')} />
  {:else if !selected}
    <ul class="entries" role="list">
      {#each suggestions as suggestion (suggestion.id)}
        <li class="proposal">
          <span class="proposal__text">
            <strong>{t(REASONS[suggestion.reason] ?? 'sameWork.reason.same_title')}</strong>
            <span class="muted">
              « {titleOf(suggestion.a)} » / « {titleOf(suggestion.b)} »
            </span>
          </span>
          <Button size="sm" onclick={() => open(suggestion)}>{t('sameWork.open')}</Button>
        </li>
      {/each}
    </ul>
  {:else}
    {@const pair = selected}
    <div class="stack-lg">
      <div class="btn-row">
        <Button variant="ghost" icon="left" onclick={back}>{t('sameWork.back')}</Button>
      </div>
      <h2 class="pair">« {titleOf(pair.a)} » / « {titleOf(pair.b)} »</h2>

      <section class="panel stack" aria-labelledby="work-plan-title">
        <h3 id="work-plan-title">{t('sameWork.plan')}</h3>
        <Field label={t('sameWork.keep')}>
          {#snippet children(control)}
            <Select {...control} bind:value={keepId}>
              <option value={pair.a}>« {titleOf(pair.a)} »</option>
              <option value={pair.b}>« {titleOf(pair.b)} »</option>
            </Select>
          {/snippet}
        </Field>
        <div class="btn-row">
          <Button onclick={planWork} disabled={busy}>{t('sameWork.open')}</Button>
        </div>
        {#if workPlan}
          <p>{t('sameWork.plan.editions', { count: workPlan.editions.length })}</p>
          {#if workPlan.blockers.length > 0}
            <Alert
              tone="danger"
              live="assertive"
              title={t('sameWork.plan.blockers', { reasons: workPlan.blockers.join(', ') })}
            />
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
            <Alert tone="success" live="polite" title={t('sameWork.applied')} />
          {:else if workPlan.mergeable}
            <div class="btn-row">
              <Button variant="danger" onclick={() => (confirming = 'works')} disabled={busy}>
                {t('sameWork.apply')}
              </Button>
            </div>
          {/if}
        {/if}
      </section>

      <section class="panel stack" aria-labelledby="edition-plan-title">
        <h3 id="edition-plan-title">{t('sameWork.editions.title')}</h3>
        <Field label={t('sameWork.editions.keep')}>
          {#snippet children(control)}
            <Select {...control} bind:value={editionKeep}>
              {#each editionOptions as e (e.edition_id)}
                <option value={e.edition_id}>{editionLabel(e)}</option>
              {/each}
            </Select>
          {/snippet}
        </Field>
        <Field label={t('sameWork.editions.drop')}>
          {#snippet children(control)}
            <Select {...control} bind:value={editionDrop}>
              {#each editionOptions as e (e.edition_id)}
                <option value={e.edition_id}>{editionLabel(e)}</option>
              {/each}
            </Select>
          {/snippet}
        </Field>
        <div class="btn-row">
          <Button onclick={planEdition} disabled={busy || !editionKeep || !editionDrop}>
            {t('sameWork.editions.plan')}
          </Button>
        </div>
        {#if editionPlan}
          <p>
            {t('sameWork.editions.moves', {
              files: editionPlan.units.length,
              volumes: editionPlan.volumes.length,
              sendings: editionPlan.bindings.length
            })}
          </p>
          {#if editionPlan.blockers.length > 0}
            <Alert
              tone="danger"
              live="assertive"
              title={t('sameWork.plan.blockers', { reasons: editionPlan.blockers.join(', ') })}
            />
          {/if}
          {#if editionPlan.tombstoned_units > 0}
            <Alert
              tone="warning"
              live="assertive"
              title={t('sameWork.editions.tombstoned_units', { count: editionPlan.tombstoned_units })}
            />
          {/if}
          {#if editionPlan.tombstoned_ids > 0}
            <Alert
              tone="warning"
              live="assertive"
              title={t('sameWork.editions.tombstoned_ids', { count: editionPlan.tombstoned_ids })}
            />
          {/if}
          {#if editionPlan.applied}
            <Alert tone="success" live="polite" title={t('sameWork.editions.applied')} />
          {:else if editionPlan.mergeable}
            <div class="btn-row">
              <Button variant="danger" onclick={() => (confirming = 'editions')} disabled={busy}>
                {t('sameWork.editions.apply')}
              </Button>
            </div>
          {/if}
        {/if}
      </section>
    </div>
  {/if}

  {#if problem}
    <div class="problem"><ProblemAlert {problem} level={2} /></div>
  {/if}
  {#if reauth.reauthMethods}
    <div class="problem">
      <ReauthPrompt
        methods={reauth.reauthMethods}
        ondone={() => reauth.afterReauth()}
        oncancel={() => reauth.cancelReauth()}
      />
    </div>
  {/if}
</main>

<ConfirmDialog
  bind:open={() => confirming !== null, (value) => { if (!value) confirming = null; }}
  title={confirming === 'editions' ? t('sameWork.editions.confirm_title') : t('sameWork.confirm_title')}
  message={confirming === 'editions' ? t('sameWork.editions.confirm_message') : t('sameWork.confirm_message')}
  confirmLabel={t('sameWork.confirm')}
  onconfirm={async () => {
    const which = confirming;
    confirming = null;
    if (which === 'editions') await applyEdition();
    else if (which === 'works') await applyWork();
  }}
/>

<style>
  .loading {
    display: grid;
    gap: var(--space-4);
  }
  .proposal {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3) var(--space-4);
    padding: var(--space-3) 0;
  }
  .proposal__text {
    display: grid;
    gap: var(--space-1);
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .pair {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .panel :global(p) {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .panel > h3 {
    margin: 0;
  }
  .problem {
    margin-top: var(--space-6);
  }
</style>
