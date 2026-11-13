<script lang="ts">
  // The series sheet (EF-SRC-037, EF-SRC-038, EF-BIB-005, EF-BIB-011, EF-BIB-019, EF-BIB-056): the
  // admin's view of one followed Edition. The server says what it allows (`can`) and the screen
  // shows no other action; every write goes through the door that already exists for it, and the
  // reserved ones (Séparer, the attach) ask for the password in place when the session is stale.
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import {
    attachConfirm,
    attachPlan,
    editionSheet,
    fieldLock,
    followCheck,
    preferredTeamSet,
    upstreamStatusSet,
    worksSeparate
  } from '#lib/api/index.ts';
  import type {
    AttachPlanOut,
    SeparateOut,
    SheetFieldOut,
    SheetOut,
    StatusIn
  } from '#lib/api/types.gen.ts';
  import Bindings from '#lib/components/Bindings.svelte';
  import Gaps from '#lib/components/Gaps.svelte';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { formatDateTime, t, type MessageKey } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, problemOf } from '#lib/triage.ts';

  type Status = StatusIn['status'];
  const STATUSES: readonly Status[] = ['ongoing', 'finished', 'paused', 'unknown'];
  const STATUS_LABELS: Record<Status, MessageKey> = {
    ongoing: 'sheet.status.ongoing',
    finished: 'sheet.status.finished',
    paused: 'sheet.status.paused',
    unknown: 'sheet.status.unknown'
  };

  const editionId = $derived(page.params.id ?? '');
  let sheet = $state<SheetOut | null>(null);
  let problem = $state<ProblemView | null>(null);
  let actionProblem = $state<ProblemView | null>(null);
  let message = $state('');
  let status = $state<Status>('unknown');
  let fieldKey = $state('');
  let fieldValue = $state('');
  let confirmingSeparate = $state(false);
  let separated = $state<SeparateOut | null>(null);
  let plan = $state<AttachPlanOut | null>(null);
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const edits = $derived<SheetFieldOut[]>(sheet?.fields ?? []);
  const merge = $derived(sheet?.merges[0] ?? null);
  const selected = $derived(edits.find((f) => `${f.on}|${f.field}|${f.lang}` === fieldKey) ?? null);

  async function load(): Promise<void> {
    const { data, error } = await editionSheet({ path: { edition_id: editionId } });
    if (error !== undefined || !data) {
      problem = problemView(error);
      sheet = null;
      return;
    }
    problem = null;
    sheet = data;
    status = data.upstream_status;
    const first = data.fields[0];
    if (!fieldKey && first) fieldKey = `${first.on}|${first.field}|${first.lang}`;
  }

  /** Run a write; a stale password asks for it in place and runs the write again after. */
  async function guarded(
    action: () => Promise<{ error?: unknown }>,
    done: () => void | Promise<void>
  ): Promise<void> {
    const { error } = await action();
    if (error === undefined) {
      actionProblem = null;
      await done();
    } else if (needsReauth(error)) {
      retry = () => guarded(action, done);
      reauthMethods = problemOf(error).reauth_methods ?? ['password'];
    } else {
      actionProblem = problemView(error);
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

  async function checkNow(): Promise<void> {
    const { data, error } = await followCheck({
      headers: headers(),
      path: { edition_id: editionId }
    });
    if (error !== undefined || !data) {
      actionProblem = problemView(error);
      return;
    }
    actionProblem = null;
    message = data.message;
  }

  const saveStatus = () =>
    guarded(
      () =>
        upstreamStatusSet({ headers: headers(), path: { edition_id: editionId }, body: { status } }),
      async () => {
        message = t('sheet.saved');
        await load();
      }
    );

  const prefer = (teamId: string) =>
    guarded(
      () =>
        preferredTeamSet({
          headers: headers(),
          path: { edition_id: editionId },
          body: { team_id: teamId }
        }),
      async () => {
        message = t('sheet.saved');
        await load();
      }
    );

  const lockField = () =>
    selected
      ? guarded(
          () =>
            fieldLock({
              headers: headers(),
              path: { edition_id: editionId, field: selected.field },
              body: { value: fieldValue, lang: selected.lang, on: selected.on }
            }),
          async () => {
            message = t('sheet.saved');
            await load();
          }
        )
      : Promise.resolve();

  const separate = (mergeId: string) =>
    guarded(
      async () => {
        const { data, error } = await worksSeparate({
          headers: headers(),
          path: { merge_id: mergeId },
          body: {}
        });
        if (data) separated = data;
        return { error };
      },
      async () => {
        confirmingSeparate = false;
        message = t('sheet.separate.done');
        await load();
      }
    );

  const planAttach = (attachId: string) =>
    guarded(
      async () => {
        const { data, error } = await attachPlan({ headers: headers(), path: { attach_id: attachId } });
        if (data) plan = data;
        return { error };
      },
      () => undefined
    );

  const confirmAttach = (attachId: string, token: string) =>
    guarded(
      () =>
        attachConfirm({
          headers: headers(),
          path: { attach_id: attachId },
          body: { confirm: token }
        }),
      async () => {
        plan = null;
        message = t('sheet.attach.done');
        await load();
      }
    );

  onMount(load);
</script>

<svelte:head>
  <title>{t('sheet.title')}</title>
</svelte:head>

<main>
  <h1>{t('sheet.title')}</h1>

  {#if problem}
    <ProblemCard {problem} />
  {:else if !sheet}
    <p>{t('sheet.loading')}</p>
  {:else}
    <header>
      {#if sheet.cover_hash}
        <img
          src={`/api/v1/thumbnails/${sheet.cover_hash}/320`}
          width="160"
          height="240"
          alt={sheet.title}
        />
      {/if}
      <h2>{sheet.title}</h2>
    </header>

    {#if message}<p role="status">{message}</p>{/if}
    {#if actionProblem}<ProblemCard problem={actionProblem} level={3} />{/if}
    {#if reauthMethods}
      <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
    {/if}

    {#if sheet.can.check}
      <button type="button" onclick={checkNow}>{t('sheet.check')}</button>
    {/if}

    <Bindings
      bindings={sheet.bindings}
      editionId={sheet.edition_id}
      canReorder={sheet.can.reorder}
      onreload={load}
    />

    {#if sheet.can.acknowledge_gap}
      <Gaps gaps={sheet.gaps} editionId={sheet.edition_id} />
    {/if}

    {#if sheet.can.set_upstream_status}
      <section aria-labelledby="status-title">
        <h2 id="status-title">{t('sheet.status.title')}</h2>
        <select bind:value={status} aria-label={t('sheet.status.title')}>
          {#each STATUSES as value (value)}
            <option {value}>{t(STATUS_LABELS[value])}</option>
          {/each}
        </select>
        <button type="button" onclick={saveStatus}>{t('sheet.status.save')}</button>
        {#if sheet.upstream_status_by === 'manual'}
          <span class="muted">{t('sheet.status.manual')}</span>
        {/if}
      </section>
    {/if}

    {#if sheet.can.prefer_team}
      <section aria-labelledby="teams-title">
        <h2 id="teams-title">{t('sheet.teams.title')}</h2>
        <ul>
          {#each sheet.teams as team (team.id)}
            <li>
              <span>{team.label}</span>
              {#if team.preferred}
                <span class="badge">{t('sheet.teams.preferred')}</span>
              {:else}
                <button type="button" onclick={() => prefer(team.id)}
                  >{t('sheet.teams.prefer', { team: team.label })}</button
                >
              {/if}
            </li>
          {/each}
        </ul>
        <p class="muted">{t('sheet.teams.note')}</p>
      </section>
    {/if}

    {#if sheet.can.edit_field && edits.length > 0}
      <section aria-labelledby="fields-title">
        <h2 id="fields-title">{t('sheet.fields.title')}</h2>
        <label>
          {t('sheet.fields.field')}
          <select
            bind:value={fieldKey}
            onchange={() => (fieldValue = selected?.value ?? '')}
          >
            {#each edits as f (`${f.on}|${f.field}|${f.lang}`)}
              <option value={`${f.on}|${f.field}|${f.lang}`}>
                {f.field} ({f.lang}, {t(f.on === 'work' ? 'sheet.fields.on_work' : 'sheet.fields.on_edition')})
              </option>
            {/each}
          </select>
        </label>
        {#if selected?.locked}
          <span class="badge">{selected.manual ? t('sheet.fields.locked') : ''}</span>
        {/if}
        <label>
          {t('sheet.fields.value')}
          <input bind:value={fieldValue} />
        </label>
        <button type="button" disabled={!selected} onclick={lockField}>{t('sheet.fields.lock')}</button>
      </section>
    {/if}

    {#if sheet.can.separate && merge}
      <section aria-labelledby="separate-title">
        <h2 id="separate-title">{t('sheet.separate.title')}</h2>
        <p>{t('sheet.separate.explain')}</p>
        <p class="muted">
          {t('sheet.separate.until', { date: formatDateTime(merge.undoable_until) })}
        </p>
        {#if confirmingSeparate}
          <button type="button" onclick={() => separate(merge.merge_id)}
            >{t('sheet.separate.confirm')}</button
          >
          <button type="button" onclick={() => (confirmingSeparate = false)}
            >{t('sheet.separate.cancel')}</button
          >
        {:else}
          <button type="button" onclick={() => (confirmingSeparate = true)}
            >{t('sheet.separate.ask')}</button
          >
        {/if}
      </section>
    {/if}

    {#if separated}
      <section aria-labelledby="plan-title">
        <h2 id="plan-title">{t('sheet.separate.plan')}</h2>
        <p>{t('sheet.separate.moves', { count: separated.series_plan.moves.length })}</p>
        <p>{t('sheet.separate.blockers', { count: separated.series_plan.blockers.length })}</p>
      </section>
    {/if}

    {#if sheet.can.confirm_attach && sheet.attach}
      <section aria-labelledby="attach-title">
        <h2 id="attach-title">{t('sheet.attach.title')}</h2>
        {#if plan}
          <p>{t('sheet.attach.withdrawals', { count: plan.withdrawals.length })}</p>
          <button type="button" onclick={() => confirmAttach(plan!.attach_id, plan!.token)}
            >{t('sheet.attach.confirm')}</button
          >
        {:else}
          <button type="button" onclick={() => planAttach(sheet!.attach!.attach_id)}
            >{t('sheet.attach.plan')}</button
          >
        {/if}
      </section>
    {/if}
  {/if}
</main>

<style>
  main {
    max-width: 48rem;
    margin: 0 auto;
    padding: 1rem;
  }
  header {
    display: flex;
    gap: 1rem;
    align-items: center;
  }
  img {
    border-radius: var(--radius-cover);
  }
  section {
    margin-top: 1.5rem;
  }
  ul {
    list-style: none;
    padding: 0;
  }
  li {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }
  button {
    min-height: 2.75rem;
  }
  .muted {
    color: var(--color-muted);
  }
  .badge {
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    border: 1px solid var(--color-rule);
  }
</style>
