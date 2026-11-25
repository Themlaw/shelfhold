<script lang="ts">
  // EF-INT-017 / EF-INT-018: the admin's control of the queue and of the maintenance mode.
  // The server decides everything: the routes are reserved to the administrator, ask a fresh session
  // where L13 asks for one, write their own audit line, and refuse every write but the exit while
  // the mode is on. This screen only shows what the server answers and offers the actions the
  // server would accept: it opens no other door, and a hidden button is never the guard.
  import { onMount } from 'svelte';
  import {
    jobCancel,
    jobPriority,
    jobReplay,
    jobsList,
    maintenanceEnter,
    maintenanceExit,
    maintenanceStatus,
    queuePause,
    queueResume
  } from '#lib/api/index.ts';
  import type { JobOut, MaintenanceStatusOut } from '#lib/api/types.gen.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import { reloadOnStream } from '#lib/live.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { isForbidden, needsReauth, needsSession, problemOf } from '#lib/triage.ts';
  import Badge, { type BadgeTone } from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';

  const PRIORITIES = ['new', 'request', 'backfill'] as const;
  const PRIORITY_LABEL: Record<(typeof PRIORITIES)[number], MessageKey> = {
    new: 'queue.priority.new',
    request: 'queue.priority.request',
    backfill: 'queue.priority.backfill'
  };
  const STATUS_LABEL: Record<string, MessageKey> = {
    queued: 'queue.status.queued',
    running: 'queue.status.running',
    paused: 'queue.status.paused',
    failed: 'queue.status.failed',
    cancelled: 'queue.status.cancelled',
    dead_letter: 'queue.status.dead_letter',
    succeeded: 'queue.status.succeeded'
  };
  const PREEMPTED = 'window.preempted'; // `error_code` of a window job stopped at the end of its window
  const SOURCE_PREFIX = 'source:';

  let status = $state<MaintenanceStatusOut | null>(null);
  let jobs = $state<JobOut[]>([]);
  let loaded = $state(false);
  let forbidden = $state(false);
  let signedOut = $state(false);
  let message = $state('');
  let failed = $state(false);
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const maintenanceActive = $derived(status?.maintenance.active ?? false);
  const pausedScopes = $derived(status?.queue.paused ?? []);
  const globalPaused = $derived(pausedScopes.includes('all'));
  const sources = $derived.by(() => {
    const names = [
      ...jobs.map((job) => job.source_key ?? ''),
      ...pausedScopes.map((scope) =>
        scope.startsWith(SOURCE_PREFIX) ? scope.slice(SOURCE_PREFIX.length) : ''
      )
    ];
    return names.filter((name, i) => name !== '' && names.indexOf(name) === i).sort();
  });

  function say(text: string, bad = false): void {
    message = text;
    failed = bad;
  }

  /** Run an action; a stale password asks for it in place and runs the action again after. */
  async function guarded(action: () => Promise<void>): Promise<void> {
    try {
      await action();
    } catch (error) {
      if (needsReauth(error)) {
        retry = action;
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else if (needsSession(error)) {
        signedOut = true;
      } else if (isForbidden(error)) {
        forbidden = true;
      } else {
        say(t('queue.error'), true);
      }
    }
  }

  async function load(): Promise<void> {
    await guarded(async () => {
      const { data: state } = await maintenanceStatus({ throwOnError: true });
      const { data: page } = await jobsList({ throwOnError: true });
      status = state;
      jobs = page.items;
      loaded = true;
    });
  }

  onMount(() => {
    void load();
    return reloadOnStream(load);
  });

  async function afterReauth(): Promise<void> {
    const again = retry;
    retry = null;
    reauthMethods = null;
    if (again) await guarded(again);
  }

  function cancelReauth(): void {
    retry = null;
    reauthMethods = null;
  }

  /** A write, then the screen is read again from the server: nothing is assumed locally. */
  function act(write: () => Promise<string | null>): Promise<void> {
    return guarded(async () => {
      const text = await write();
      say(text ?? '');
      await load();
    });
  }

  const toggleMaintenance = () =>
    act(async () => {
      if (maintenanceActive) await maintenanceExit({ headers: headers(), throwOnError: true });
      else await maintenanceEnter({ headers: headers(), throwOnError: true });
      return null;
    });

  const pause = (source: string | null) =>
    act(async () => {
      await queuePause({ headers: headers(), body: { source }, throwOnError: true });
      return null;
    });

  const resume = (source: string | null) =>
    act(async () => {
      await queueResume({ headers: headers(), body: { source }, throwOnError: true });
      return null;
    });

  const cancel = (job: JobOut) =>
    act(async () => {
      const { data } = await jobCancel({
        headers: headers(),
        path: { job_id: job.id },
        throwOnError: true
      });
      return t(
        data.outcome === 'cancelled'
          ? 'queue.cancelled'
          : data.outcome === 'cancelling'
            ? 'queue.cancelling'
            : 'queue.too_late'
      );
    });

  const setPriority = (job: JobOut, priority_class: string) =>
    act(async () => {
      await jobPriority({
        headers: headers(),
        path: { job_id: job.id },
        body: { priority_class },
        throwOnError: true
      });
      return null;
    });

  const replay = (job: JobOut) =>
    act(async () => {
      const { data } = await jobReplay({
        headers: headers(),
        path: { job_id: job.id },
        throwOnError: true
      });
      return t(data.replayed ? 'queue.replayed' : 'queue.not_replayed');
    });

  const isPreempted = (job: JobOut): boolean =>
    job.status === 'paused' && job.error_code === PREEMPTED;
  const canCancel = (job: JobOut): boolean => ['queued', 'running', 'paused'].includes(job.status);
  const canReplay = (job: JobOut): boolean =>
    ['failed', 'cancelled', 'dead_letter'].includes(job.status);
  const statusLabel = (value: string): string =>
    STATUS_LABEL[value] ? t(STATUS_LABEL[value]) : value;
  const shortId = (id: string): string => id.slice(-8);
  const STATUS_TONE: Record<string, BadgeTone> = {
    running: 'accent',
    succeeded: 'success',
    failed: 'danger',
    dead_letter: 'danger',
    paused: 'warning',
    cancelled: 'neutral',
    queued: 'neutral'
  };
</script>

<section class="queue-controls stack-lg" aria-label={t('queue.title')}>
  {#if forbidden}
    <Alert tone="danger" title={t('queue.admin_only')} live="assertive" />
  {:else if signedOut}
    <Alert tone="warning" title={t('queue.signed_out')} live="assertive" />
  {:else if !loaded}
    <div aria-busy="true">
      <p role="status">{t('queue.loading')}</p>
      <Skeleton shape="block" /><Skeleton /><Skeleton short />
    </div>
  {:else}
    <div class="panel maintenance" data-testid="maintenance-toggle">
      <div class="maintenance__text">
        <h3>{t('maintenance.title')}</h3>
        <p role="status" class="maintenance__state" class:frozen={maintenanceActive}>
          {maintenanceActive ? t('maintenance.active') : t('maintenance.inactive')}
          {#if maintenanceActive}
            <Badge tone="warning" icon="pause">{t('maintenance.frozen')}</Badge>
          {/if}
        </p>
        {#if maintenanceActive}
          <p class="muted">{t('queue.frozen_notice')}</p>
          {#if (status?.maintenance.running_jobs ?? 0) > 0}
            <p class="muted">{t('maintenance.running', { count: status?.maintenance.running_jobs ?? 0 })}</p>
          {/if}
        {/if}
      </div>
      <Button variant={maintenanceActive ? 'primary' : 'secondary'} onclick={toggleMaintenance}>
        {maintenanceActive ? t('maintenance.exit') : t('maintenance.enter')}
      </Button>
    </div>

    <div class="global btn-row" data-testid="queue-global">
      {#if globalPaused}
        <Button variant="primary" icon="play" disabled={maintenanceActive} onclick={() => resume(null)}>
          {t('queue.global_resume')}
        </Button>
      {:else}
        <Button icon="pause" disabled={maintenanceActive} onclick={() => pause(null)}>
          {t('queue.global_pause')}
        </Button>
      {/if}
      {#if (status?.queue.parked_jobs ?? 0) > 0}
        <span class="muted num">{t('queue.paused_jobs', { count: status?.queue.parked_jobs ?? 0 })}</span>
      {/if}
    </div>

    {#if sources.length > 0}
      <div class="sources" data-testid="queue-sources">
        <div class="section-head"><h3>{t('queue.sources')}</h3></div>
        <ul class="entries">
          {#each sources as source (source)}
            <li class="row">
              <span class="row__name">{source}</span>
              {#if pausedScopes.includes(`${SOURCE_PREFIX}${source}`)}
                <Badge tone="warning" icon="pause">{t('queue.source_paused')}</Badge>
                <Button size="sm" disabled={maintenanceActive} onclick={() => resume(source)}>
                  {t('queue.resume_source', { source })}
                </Button>
              {:else}
                <Button size="sm" variant="ghost" disabled={maintenanceActive} onclick={() => pause(source)}>
                  {t('queue.pause_source', { source })}
                </Button>
              {/if}
            </li>
          {/each}
        </ul>
      </div>
    {/if}

    {#if jobs.length === 0}
      <EmptyState title={t('queue.empty')} art={false} />
    {:else}
      <ul class="entries jobs" data-testid="queue-jobs">
        {#each jobs as job (job.id)}
          <li class="row" class:preempted={isPreempted(job)} data-testid="queue-job">
            <span class="row__name kind">{job.kind}</span>
            <Badge tone={STATUS_TONE[job.status] ?? 'neutral'}>{statusLabel(job.status)}</Badge>
            {#if job.request_id}
              <span class="muted">{t('queue.for_whom', { request: shortId(job.request_id) })}</span>
            {/if}
            {#if isPreempted(job)}
              <span class="preempted-note muted">{t('queue.preempted')}</span>
            {/if}
            <span class="row__actions">
              {#if job.status === 'queued'}
                <Select
                  inline
                  aria-label={t('queue.priority', { kind: job.kind })}
                  value={job.priority_class}
                  disabled={maintenanceActive}
                  onchange={(e: Event) => setPriority(job, (e.target as HTMLSelectElement).value)}
                  options={PRIORITIES.map((choice) => ({ value: choice, label: t(PRIORITY_LABEL[choice]) }))}
                />
              {/if}
              {#if canCancel(job)}
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={maintenanceActive}
                  aria-label={t('queue.cancel_job', { kind: job.kind })}
                  onclick={() => cancel(job)}>{t('queue.cancel')}</Button
                >
              {/if}
              {#if canReplay(job)}
                <Button
                  size="sm"
                  icon="refresh"
                  disabled={maintenanceActive}
                  aria-label={t('queue.replay_job', { kind: job.kind })}
                  onclick={() => replay(job)}
                  >{job.status === 'dead_letter' ? t('queue.replay_verify') : t('queue.replay')}</Button
                >
              {/if}
            </span>
          </li>
        {/each}
      </ul>
    {/if}

    {#if message}
      <Alert tone={failed ? 'danger' : 'success'} title={message} live={failed ? 'assertive' : 'polite'} />
    {/if}
  {/if}

  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
  {/if}
</section>

<style>
  .maintenance {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-4);
  }
  .maintenance h3 {
    margin: 0 0 var(--space-1);
    font-size: var(--step-1);
  }
  .maintenance p {
    margin: 0;
  }
  .maintenance__state {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }
  .frozen {
    font-weight: 600;
  }
  .sources .entries {
    border-top: 0;
  }
  .sources h3 {
    margin: 0;
    font-size: var(--step-1);
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2) var(--space-3);
    padding: var(--space-3) 0;
  }
  .row__name {
    flex: 1 1 10rem;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .kind {
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: var(--step--1);
  }
  .row__actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin-inline-start: auto;
  }
  .preempted-note {
    font-style: italic;
  }
</style>
