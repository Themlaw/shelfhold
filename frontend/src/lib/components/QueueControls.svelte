<script lang="ts">
  // EF-INT-017 / EF-INT-018: pilotage de la file et bascule du mode maintenance, pour l'admin.
  // Tout vient du serveur (`GET /api/v1/maintenance`, `GET /api/v1/jobs`) ; l'écran ne décide rien.
  import { onMount } from 'svelte';
  import {
    maintenanceStatus,
    maintenanceEnter,
    maintenanceExit,
    jobsList,
    queuePause,
    queueResume,
    jobCancel,
    jobPriority,
    jobReplay
  } from '#lib/api/index.ts';
  import type {
    JobOut,
    MaintenanceStatusOut,
    PausedOut,
    ResumedOut
  } from '#lib/api/types.gen.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { t } from '#lib/i18n/index.ts';

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  let status = $state<MaintenanceStatusOut | null>(null);
  let jobs = $state<JobOut[]>([]);
  let loaded = $state(false);
  let message = $state('');

  const priorityClasses = ['new', 'request', 'backfill'] as const;

  async function load(): Promise<void> {
    try {
      const { data: s } = await maintenanceStatus();
      status = s ?? null;
      const { data: j } = await jobsList();
      jobs = j?.items ?? [];
      message = '';
    } catch {
      message = t('queue.loading');
    }
    loaded = true;
  }

  onMount(() => {
    void load();
  });

  const maintenanceActive = $derived(status?.maintenance.active ?? false);
  const pausedScopes = $derived(status?.queue.paused ?? []);
  const globalPaused = $derived(pausedScopes.includes('all'));
  const sources = $derived.by(() => {
    const set = new Set<string>();
    for (const job of jobs) if (job.source_key) set.add(job.source_key);
    for (const scope of pausedScopes) {
      if (scope.startsWith('source:')) set.add(scope.slice('source:'.length));
    }
    return [...set].sort();
  });

  async function toggleMaintenance(): Promise<void> {
    try {
      if (maintenanceActive) await maintenanceExit({ headers: headers() });
      else await maintenanceEnter({ headers: headers() });
      await load();
    } catch {
      message = t('maintenance.active');
    }
  }

  async function pauseGlobal(): Promise<void> {
    await queuePause({ headers: headers(), body: { source: null } });
    await load();
  }

  async function resumeGlobal(): Promise<void> {
    await queueResume({ headers: headers(), body: { source: null } });
    await load();
  }

  async function pauseSource(source: string): Promise<void> {
    await queuePause({ headers: headers(), body: { source } });
    await load();
  }

  async function resumeSource(source: string): Promise<void> {
    await queueResume({ headers: headers(), body: { source } });
    await load();
  }

  async function cancelJob(id: string): Promise<void> {
    await jobCancel({ headers: headers(), path: { job_id: id } });
    await load();
  }

  async function setPriority(id: string, priority_class: string): Promise<void> {
    await jobPriority({ headers: headers(), path: { job_id: id }, body: { priority_class } });
    await load();
  }

  async function replayJob(id: string): Promise<void> {
    await jobReplay({ headers: headers(), path: { job_id: id } });
    await load();
  }

  function isPreempted(job: JobOut): boolean {
    return job.error_code === 'window.preempted' || job.status === 'paused' && job.error_code === 'window.preempted';
  }
</script>

<section class="queue-controls" aria-labelledby="queue-heading">
  <h2 id="queue-heading">{t('queue.title')}</h2>

  {#if !loaded}
    <p>{t('queue.loading')}</p>
  {:else}
    <!-- Maintenance toggle -->
    <div class="maintenance" data-testid="maintenance-toggle">
      <h3>{t('maintenance.title')}</h3>
      <p role="status" class={maintenanceActive ? 'frozen' : ''}>
        {maintenanceActive ? t('maintenance.active') : t('maintenance.inactive')}
        {#if maintenanceActive}
          <span class="frozen-badge">{t('maintenance.frozen')}</span>
        {/if}
      </p>
      <button type="button" onclick={toggleMaintenance}>
        {maintenanceActive ? t('maintenance.exit') : t('maintenance.enter')}
      </button>
    </div>

    <!-- Global queue control -->
    <div class="global" data-testid="queue-global">
      {#if globalPaused}
        <button type="button" onclick={resumeGlobal}>{t('queue.global_resume')}</button>
      {:else}
        <button type="button" onclick={pauseGlobal}>{t('queue.global_pause')}</button>
      {/if}
      {#if (status?.queue.parked_jobs ?? 0) > 0}
        <span>{t('queue.paused_jobs', { count: status?.queue.parked_jobs ?? 0 })}</span>
      {/if}
    </div>

    <!-- Sources -->
    {#if sources.length > 0}
      <div class="sources" data-testid="queue-sources">
        <h3>{t('queue.sources')}</h3>
        <ul>
          {#each sources as source (source)}
            <li>
              <span>{source}</span>
              {#if pausedScopes.includes(`source:${source}`)}
                <button type="button" onclick={() => resumeSource(source)}>{t('queue.resume_source')}</button>
              {:else}
                <button type="button" onclick={() => pauseSource(source)}>{t('queue.pause_source')}</button>
              {/if}
            </li>
          {/each}
        </ul>
      </div>
    {/if}

    <!-- Jobs -->
    {#if jobs.length === 0}
      <p>{t('queue.empty')}</p>
    {:else}
      <ul class="jobs" data-testid="queue-jobs">
        {#each jobs as job (job.id)}
          <li class:preempted={isPreempted(job)} data-testid="queue-job">
            <span class="kind">{job.kind}</span>
            <span class="status">{job.status}</span>
            {#if job.request_id}
              <span class="for-whom">{t('queue.for_whom', { who: job.request_id })}</span>
            {/if}
            {#if isPreempted(job)}
              <span class="preempted">{t('queue.preempted')}</span>
            {/if}
            <button type="button" onclick={() => cancelJob(job.id)}>{t('queue.cancel')}</button>
            <select
              aria-label={t('queue.priority')}
              value={job.priority_class ?? 'new'}
              onchange={(e) => setPriority(job.id, (e.target as HTMLSelectElement).value)}
            >
              {#each priorityClasses as pc (pc)}
                <option value={pc}>{pc}</option>
              {/each}
            </select>
            <button type="button" onclick={() => replayJob(job.id)}>{t('queue.replay')}</button>
            {#if job.status === 'dead_letter'}
              <button type="button" onclick={() => replayJob(job.id)}>{t('queue.replay_verify')}</button>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}

    {#if message}
      <p role="status">{message}</p>
    {/if}
  {/if}
</section>

<style>
  .queue-controls {
    max-width: 48rem;
    margin: 0 auto;
    padding: 1rem;
  }
  .maintenance, .global, .sources, .jobs {
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
  .jobs {
    list-style: none;
    padding: 0;
  }
  .jobs li {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--color-rule, #eee);
  }
  .preempted {
    font-style: italic;
    color: var(--color-muted);
  }
  button {
    padding: 0.25rem 0.5rem;
  }
</style>