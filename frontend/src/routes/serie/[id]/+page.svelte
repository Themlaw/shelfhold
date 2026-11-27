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
    UpstreamStatusIn
  } from '#lib/api/types.gen.ts';
  import Bindings from '#lib/components/Bindings.svelte';
  import Gaps from '#lib/components/Gaps.svelte';
  import ProblemAlert, { refusalOf } from '#lib/ui/molecules/ProblemAlert.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { formatDateTime, t, uiLanguage, type MessageKey } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, problemOf } from '#lib/triage.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Cover from '#lib/ui/atoms/Cover.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Tabs from '#lib/ui/molecules/Tabs.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import { roleOf } from '#lib/nav.ts';
  import BundleSlices from './BundleSlices.svelte';
  import Chapters from './Chapters.svelte';
  import Exports from './Exports.svelte';
  import OfflineButton from './OfflineButton.svelte';
  import ReadButton from './ReadButton.svelte';
  import ReadMarks from './ReadMarks.svelte';
  import RelatedWorks from './RelatedWorks.svelte';
  import SupportAuthor from './SupportAuthor.svelte';
  import SwitchReport from './SwitchReport.svelte';

  type Status = UpstreamStatusIn['status'];
  const STATUSES: readonly Status[] = ['ongoing', 'finished', 'paused', 'unknown'];
  const STATUS_LABELS: Record<Status, MessageKey> = {
    ongoing: 'sheet.status.ongoing',
    finished: 'sheet.status.finished',
    paused: 'sheet.status.paused',
    unknown: 'sheet.status.unknown'
  };

  /** The name of a language in the interface language; the code itself when it is not a valid tag. */
  function languageName(code: string): string {
    try {
      return new Intl.DisplayNames([uiLanguage()], { type: 'language' }).of(code) ?? code;
    } catch {
      return code;
    }
  }

  const editionId = $derived(page.params.id ?? '');
  // the exports are the admin's: the other roles do not see the tab, and the route is not asked
  const canExports = $derived(roleOf((page.data as { user?: { role?: unknown } } | undefined)?.user?.role) === 'admin');
  let sheet = $state<SheetOut | null>(null);
  let readScope = $state<'full' | 'excerpt' | null>(null); // what the read button found: a copy is kept only for a full read
  let problem = $state<ProblemView | null>(null);
  let actionProblem = $state<ProblemView | null>(null);
  let message = $state('');
  let tab = $state('sources');
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
  <title>{sheet ? `${sheet.title} · ${t('sheet.title')}` : t('sheet.title')}</title>
</svelte:head>

<main class="page sheet">
  {#if problem}
    <div class="running-head"><span>{t('sheet.title')}</span></div>
    <h1 class="visually-hidden">{t('sheet.title')}</h1>
    <ProblemAlert {problem} kind={refusalOf(problem)} />
  {:else if !sheet}
    <div class="running-head"><span>{t('sheet.title')}</span></div>
    <h1 class="visually-hidden">{t('sheet.title')}</h1>
    <div class="page-head" role="status" aria-busy="true">
      <span class="visually-hidden">{t('sheet.loading')}</span>
      <div class="sheet__cover"><Skeleton shape="cover" /></div>
      <div class="page-head__text sheet__skeleton">
        <Skeleton />
        <Skeleton short />
        <Skeleton shape="block" />
      </div>
    </div>
  {:else}
    <div class="running-head">
      <nav aria-label={t('sheet.title')}>
        <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
        <a href="/bibliotheque">{t('nav.library')}</a><span class="crumb-sep" aria-hidden="true">›</span><span
          aria-current="page">{sheet.title}</span
        >
      </nav>
    </div>
    <div class="page-head sheet__head">
      <div class="sheet__cover">
        <Cover
          title={sheet.title}
          seed={sheet.work_id}
          src={sheet.cover_hash ? `/api/v1/thumbnails/${sheet.cover_hash}/320` : null}
          size="fill"
        />
      </div>
      <div class="page-head__text">
        <h1 class="page-title page-title--work sheet__title">{sheet.title}</h1>
        <p class="sheet__meta">
          <Badge>{languageName(sheet.lang)}</Badge>
          {#if sheet.upstream_status !== 'unknown'}
            <Badge>{t(STATUS_LABELS[sheet.upstream_status])}</Badge>
          {/if}
          {#if sheet.followed}<Badge>{t('sheet.followed')}</Badge>{/if}
        </p>
        <div class="btn-row sheet__actions">
          <ReadButton unitId={sheet.first_unit_id} bind:scope={readScope} />
          {#if sheet.can.check}
            <Button variant="secondary" size="lg" onclick={checkNow}>{t('sheet.check')}</Button>
          {/if}
        </div>
        {#if !sheet.first_unit_id}
          <p class="hint sheet__empty">
            {t('sheet.no_unit')}
            {sheet.can.check ? t('sheet.no_unit_check') : t('sheet.no_unit_sources')}
          </p>
        {/if}
        {#if readScope === 'full'}
          <div class="sheet__offline"><OfflineButton unitId={sheet.first_unit_id} /></div>
        {/if}
      </div>
    </div>

    <div class="stack">
      {#if message}<Alert tone="success" title={message} live="polite" />{/if}
      {#if actionProblem}<ProblemAlert problem={actionProblem} level={3} />{/if}
      {#if reauthMethods}
        <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
      {/if}
    </div>

    <Tabs
      bind:value={tab}
      label={t('sheet.tabs.label')}
      items={[
        { value: 'sources', label: t('sheet.tab.sources') },
        { value: 'chapters', label: t('sheet.tab.chapters') },
        {
          value: 'reading',
          label: t('sheet.tab.reading'),
          short: t('sheet.tab.reading_short')
        },
        ...(canExports ? [{ value: 'exports', label: t('sheet.tab.exports') }] : []),
        { value: 'manage', label: t('sheet.tab.manage') }
      ]}
    >
      {#snippet panel(item)}
        {#if item.value === 'sources'}
          <Bindings
            bindings={sheet!.bindings}
            editionId={sheet!.edition_id}
            canReorder={sheet!.can.reorder}
            onreload={load}
          />
          {#if sheet!.bindings.some((b) => !b.active)}
            <p class="section">
              <Button href={`/serie/${sheet!.edition_id}/migrer`} variant="secondary">{t('migrate.title')}</Button>
            </p>
          {/if}
          {#if sheet!.can.acknowledge_gap}
            <Gaps gaps={sheet!.gaps} editionId={sheet!.edition_id} />
          {/if}
        {:else if item.value === 'chapters'}
          <Chapters editionId={sheet!.edition_id} />
        {:else if item.value === 'exports'}
          <Exports editionId={sheet!.edition_id} />
        {:else if item.value === 'reading'}
          <BundleSlices editionId={sheet!.edition_id} />
          <ReadMarks editionId={sheet!.edition_id} />
          <SupportAuthor editionId={sheet!.edition_id} />
        {:else}
          {@render manage(sheet!)}
        {/if}
      {/snippet}
    </Tabs>
  {/if}
</main>

{#snippet manage(sheet: SheetOut)}
  <RelatedWorks workId={sheet.work_id} canEdit />

  {#if sheet.can.set_upstream_status}
    <section class="section" aria-labelledby="status-title">
      <div class="section-head"><h2 id="status-title">{t('sheet.status.title')}</h2></div>
      <div class="btn-row">
        <Select
          bind:value={status}
          aria-label={t('sheet.status.title')}
          inline
          options={STATUSES.map((value) => ({ value, label: t(STATUS_LABELS[value]) }))}
        />
        <Button variant="secondary" onclick={saveStatus}>{t('sheet.status.save')}</Button>
        {#if sheet.upstream_status_by === 'manual'}
          <Badge>{t('sheet.status.manual')}</Badge>
        {/if}
      </div>
    </section>
  {/if}

  {#if sheet.can.prefer_team}
    <section class="section" aria-labelledby="teams-title">
      <div class="section-head"><h2 id="teams-title">{t('sheet.teams.title')}</h2></div>
      <ul class="entries">
        {#each sheet.teams as team (team.id)}
          <li class="sheet__row">
            <span class="sheet__row-text">{team.label}</span>
            {#if team.preferred}
              <Badge tone="accent">{t('sheet.teams.preferred')}</Badge>
            {:else}
              <Button variant="secondary" size="sm" onclick={() => prefer(team.id)}
                >{t('sheet.teams.prefer', { team: team.label })}</Button
              >
            {/if}
          </li>
        {/each}
      </ul>
      <p class="hint">{t('sheet.teams.note')}</p>
    </section>
    <!-- the second step: replace what is already served (a reserved action, a fresh session) -->
    <SwitchReport {editionId} />
  {/if}

  {#if sheet.can.edit_field && edits.length > 0}
    <section class="section" aria-labelledby="fields-title">
      <div class="section-head"><h2 id="fields-title">{t('sheet.fields.title')}</h2></div>
      <div class="stack sheet__form">
        <Field label={t('sheet.fields.field')}>
          {#snippet children(control)}
            <Select
              {...control}
              bind:value={fieldKey}
              onchange={() => (fieldValue = selected?.value ?? '')}
              options={edits.map((f) => ({
                value: `${f.on}|${f.field}|${f.lang}`,
                label: `${f.field} (${f.lang}, ${t(f.on === 'work' ? 'sheet.fields.on_work' : 'sheet.fields.on_edition')})`
              }))}
            />
          {/snippet}
        </Field>
        {#if selected?.locked && selected.manual}
          <Badge icon="lock">{t('sheet.fields.locked')}</Badge>
        {/if}
        <Field label={t('sheet.fields.value')}>
          {#snippet children(control)}
            <Input {...control} bind:value={fieldValue} />
          {/snippet}
        </Field>
        <Button variant="primary" disabled={!selected} onclick={lockField}>{t('sheet.fields.lock')}</Button>
      </div>
    </section>
  {/if}

  {#if sheet.can.separate && merge}
    <section class="section" aria-labelledby="separate-title">
      <div class="section-head"><h2 id="separate-title">{t('sheet.separate.title')}</h2></div>
      <p>{t('sheet.separate.explain')}</p>
      <p class="hint">
        {t('sheet.separate.until', { date: formatDateTime(merge.undoable_until) })}
      </p>
      <div class="btn-row">
        {#if confirmingSeparate}
          <Button variant="danger" onclick={() => separate(merge.merge_id)}
            >{t('sheet.separate.confirm')}</Button
          >
          <Button variant="ghost" onclick={() => (confirmingSeparate = false)}
            >{t('sheet.separate.cancel')}</Button
          >
        {:else}
          <Button variant="secondary" onclick={() => (confirmingSeparate = true)}
            >{t('sheet.separate.ask')}</Button
          >
        {/if}
      </div>
    </section>
  {/if}

  {#if separated}
    <section class="section" aria-labelledby="plan-title">
      <div class="section-head"><h2 id="plan-title">{t('sheet.separate.plan')}</h2></div>
      <p>{t('sheet.separate.moves', { count: separated.series_plan.moves.length })}</p>
      <p>{t('sheet.separate.blockers', { count: separated.series_plan.blockers.length })}</p>
    </section>
  {/if}

  {#if sheet.can.confirm_attach && sheet.attach}
    <section class="section" aria-labelledby="attach-title">
      <div class="section-head"><h2 id="attach-title">{t('sheet.attach.title')}</h2></div>
      {#if plan}
        <p>{t('sheet.attach.withdrawals', { count: plan.withdrawals.length })}</p>
        <Button variant="primary" onclick={() => confirmAttach(plan!.attach_id, plan!.token)}
          >{t('sheet.attach.confirm')}</Button
        >
      {:else}
        <Button variant="secondary" onclick={() => planAttach(sheet!.attach!.attach_id)}
          >{t('sheet.attach.plan')}</Button
        >
      {/if}
    </section>
  {/if}
{/snippet}

<style>
  .sheet__head,
  .sheet__skeleton {
    align-items: start;
  }
  .sheet__cover {
    flex: 0 0 auto;
    width: min(100%, 14rem);
  }
  .sheet__title {
    overflow-wrap: anywhere;
  }
  .sheet__actions {
    margin-top: var(--space-5);
  }
  /* the way back is a link on its own: a 44 px target (ENF-A11Y-004) */
  .running-head a {
    display: inline-flex;
    align-items: center;
    min-block-size: var(--target);
    min-inline-size: var(--target);
  }
  .crumb-sep {
    margin-inline: var(--space-2);
  }
  .sheet__meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin: var(--space-3) 0 0;
  }
  .sheet__empty {
    max-width: var(--measure);
    margin-top: var(--space-3);
  }
  .sheet__offline {
    margin-top: var(--space-3);
  }
  .sheet__skeleton {
    display: grid;
    gap: var(--space-3);
  }
  .sheet__form {
    max-width: var(--form-max);
  }
  .sheet__row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    min-height: var(--target);
    padding: var(--space-2) 0;
  }
  .sheet__row-text {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  @media (max-width: 40rem) {
    .sheet__cover {
      width: min(60%, 11rem);
    }
  }
</style>
