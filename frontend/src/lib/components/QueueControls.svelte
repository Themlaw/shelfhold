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
</script>

<section class="queue-controls" aria-label={t('queue.title')}>
  {#if forbidden}
    <p role="alert">{t('queue.admin_only')}</p>
  {:else if signedOut}
    <p role="alert">{t('queue.signed_out')}</p>
  {:else if !loaded}
    <p>{t('queue.loading')}</p>
  {:else}
    <div class="maintenance" data-testid="maintenance-toggle">
      <h3>{t('maintenance.title')}</h3>
      <p role="status" class={maintenanceActive ? 'frozen' : ''}>
        {maintenanceActive ? t('maintenance.active') : t('maintenance.inactive')}
        {#if maintenanceActive}
          <span class="frozen-badge">{t('maintenance.frozen')}</span>
        {/if}
      </p>
      {#if maintenanceActive}
        <p>{t('queue.frozen_notice')}</p>
        {#if (status?.maintenance.running_jobs ?? 0) > 0}
          <p>{t('maintenance.running', { count: status?.maintenance.running_jobs ?? 0 })}</p>
        {/if}
      {/if}
      <button type="button" onclick={toggleMaintenance}>
        {maintenanceActive ? t('maintenance.exit') : t('maintenance.enter')}
      </button>
    </div>

    <div class="global" data-testid="queue-global">
      {#if globalPaused}
        <button type="button" disabled={maintenanceActive} onclick={() => resume(null)}>
          {t('queue.global_resume')}
        </button>
      {:else}
        <button type="button" disabled={maintenanceActive} onclick={() => pause(null)}>
          {t('queue.global_pause')}
        </button>
      {/if}
      {#if (status?.queue.parked_jobs ?? 0) > 0}
        <span>{t('queue.paused_jobs', { count: status?.queue.parked_jobs ?? 0 })}</span>
      {/if}
    </div>

    {#if sources.length > 0}
      <div class="sources" data-testid="queue-sources">
        <h3>{t('queue.sources')}</h3>
        <ul>
          {#each sources as source (source)}
            <li>
              <span>{source}</span>
              {#if pausedScopes.includes(`${SOURCE_PREFIX}${source}`)}
                <span>({t('queue.source_paused')})</span>
                <button type="button" disabled={maintenanceActive} onclick={() => resume(source)}>
                  {t('queue.resume_source', { source })}
                </button>
              {:else}
                <button type="button" disabled={maintenanceActive} onclick={() => pause(source)}>
                  {t('queue.pause_source', { source })}
                </button>
              {/if}
            </li>
          {/each}
        </ul>
      </div>
    {/if}

    {#if jobs.length === 0}
      <p>{t('queue.empty')}</p>
    {:else}
      <ul class="jobs" data-testid="queue-jobs">
        {#each jobs as job (job.id)}
          <li class:preempted={isPreempted(job)} data-testid="queue-job">
            <span class="kind">{job.kind}</span>
            <span class="status">{statusLabel(job.status)}</span>
            {#if job.request_id}
              <span class="for-whom">{t('queue.for_whom', { request: shortId(job.request_id) })}</span>
            {/if}
            {#if isPreempted(job)}
              <span class="preempted-note">{t('queue.preempted')}</span>
            {/if}
            {#if job.status === 'queued'}
              <select
                aria-label={t('queue.priority', { kind: job.kind })}
                value={job.priority_class}
                disabled={maintenanceActive}
                onchange={(e) => setPriority(job, (e.target as HTMLSelectElement).value)}
              >
                {#each PRIORITIES as choice (choice)}
                  <option value={choice}>{t(PRIORITY_LABEL[choice])}</option>
                {/each}
              </select>
            {/if}
            {#if canCancel(job)}
              <button
                type="button"
                disabled={maintenanceActive}
                aria-label={t('queue.cancel_job', { kind: job.kind })}
                onclick={() => cancel(job)}>{t('queue.cancel')}</button
              >
            {/if}
            {#if canReplay(job)}
              <button
                type="button"
                disabled={maintenanceActive}
                aria-label={t('queue.replay_job', { kind: job.kind })}
                onclick={() => replay(job)}
                >{job.status === 'dead_letter' ? t('queue.replay_verify') : t('queue.replay')}</button
              >
            {/if}
          </li>
        {/each}
      </ul>
    {/if}

    {#if message}
      <p role="status" class:failed>{message}</p>
    {/if}
  {/if}

  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
  {/if}
</section>

<style>
  .queue-controls {
    max-width: 48rem;
    margin: 0 auto;
    padding: 1rem;
  }
  .maintenance,
  .global,
  .sources,
  .jobs {
    margin: 1rem 0;
  }
  .frozen {
    color: var(--color-err, #c00);
    font-weight: 600;
  }
  .frozen-badge {
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    border: 1px solid var(--color-err, #c00);
  }
  .jobs,
  .sources ul {
    list-style: none;
    padding: 0;
  }
  .jobs li,
  .sources li {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--color-rule, #eee);
  }
  .preempted-note {
    font-style: italic;
  }
  .failed {
    color: var(--color-err, #c00);
  }
</style>
