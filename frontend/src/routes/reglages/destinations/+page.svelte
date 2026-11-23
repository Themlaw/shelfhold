<script lang="ts">
  // The destinations screen (EF-EXP-031): remove a destination or detach Kavita, each behind a
  // preview that says what would be lost, a phrase the server gives and the admin types, and the
  // fresh-session prompt. The server is the gate: it checks the phrase, the session and the role
  // again; nothing here decides for it.
  //
  // M4 L34: a removal that takes the files and the detachment are background jobs. The server
  // answers 202 with the id of the job: the screen says the work is "peut-être en cours" and
  // « Recharger » asks the job and the list again. The password of the Kavita account leaves the
  // field before the request goes and is never kept (the job is handed it in memory, once).
  import { onMount } from 'svelte';
  import { client } from '#lib/api/client.gen.ts';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import TypedConfirm from '#lib/components/TypedConfirm.svelte';
  import { csrfToken } from '#lib/passkeys.ts';
  import { isForbidden, needsReauth, problemOf } from '#lib/triage.ts';

  type Destination = {
    id: string;
    shelf_name: string;
    target_id: string;
    root_subpath: string;
    sync_progress?: boolean; // M4 L46: the progress sync of the binding
    can_sync_progress?: boolean; // only a destination that keeps a reading position per account
  };
  type Line = { code: string; count: number };
  type RemovalPreview = { phrase: string; warnings: Line[] };
  type RemovalReport = { complete: boolean; left: { code: string }[] };
  type JobAccepted = { job_id: string; status_url: string };
  type DetachPreview = { phrase: string; warnings: Line[] };
  type Job = {
    kind: 'removal' | 'detach';
    url: string;
    status: string;
    error: string | null;
  };

  const WARNINGS: Record<string, MessageKey> = {
    'removal:files_kept': 'destinations.warn.removal_files_kept',
    'removal:manifest_forgotten': 'destinations.warn.removal_manifest_forgotten',
    'removal:files_deleted': 'destinations.removal.files_deleted',
    'removal:past_the_bin': 'destinations.warn.removal_past_the_bin',
    'removal:adopted_kept': 'destinations.warn.removal_adopted_kept',
    'removal:indexed_elsewhere': 'destinations.warn.removal_indexed_elsewhere',
    'detach:files_kept': 'destinations.warn.detach_files_kept',
    'detach:hidden_erased': 'destinations.detach.hidden_erased',
    'detach:key_revoked': 'destinations.warn.detach_key_revoked',
    'detach:manifest_forgotten': 'destinations.warn.detach_manifest_forgotten',
    'detach:hidden_data_present': 'destinations.warn.detach_hidden_data_present',
    'detach:root_shared': 'destinations.warn.detach_root_shared',
    'detach:follows_active': 'destinations.warn.detach_follows_active',
    'detach:tombstone_journal_erased': 'destinations.warn.detach_tombstone_journal_erased'
  };
  let destinations = $state<Destination[]>([]);
  let adminOnly = $state(false);
  let notice = $state<{ text: string; kind: 'status' | 'alert' } | null>(null);

  let removing = $state<Destination | null>(null);
  let removeMode = $state<'keep_files' | 'delete_files'>('keep_files');
  let removePreview = $state<RemovalPreview | null>(null);

  let detaching = $state<Destination | null>(null);
  let detachPreview = $state<DetachPreview | null>(null);
  let detachPassword = $state('');
  let discardHidden = $state(false);
  let discardJournal = $state(false);
  let keyGone = $state(false);
  let job = $state<Job | null>(null); // the background job of the last order, if any

  let leftCount = $state(0); // files the removal left on disk, said next to its outcome
  let busy = $state(false);
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  /** « Détacher » is the target's: every shelf bound to it goes with it. */
  const concerned = (dest: Destination): string =>
    destinations
      .filter((other) => other.target_id === dest.target_id)
      .map((other) => other.shelf_name)
      .join(', ');

  const warningText = (line: Line): string => {
    const key = WARNINGS[line.code];
    return key ? t(key, { count: line.count }) : t('destinations.warn.unknown', line);
  };
  function say(text: string, kind: 'status' | 'alert'): void {
    notice = { text, kind };
  }

  /** The server asked for a fresh session: ask in place, then replay the same call. */
  function askAgain(error: unknown, replay: () => Promise<void>): boolean {
    if (!needsReauth(error)) return false;
    retry = replay;
    reauthMethods = problemOf(error).reauth_methods ?? ['password'];
    return true;
  }

  async function load(): Promise<void> {
    const { data, error } = await client.get({ url: '/api/v1/destinations', throwOnError: false });
    if (error) {
      adminOnly = isForbidden(error);
      say(adminOnly ? t('screens.admin_only') : t('destinations.error'), 'alert');
      return;
    }
    destinations = (data as { items: Destination[] }).items;
  }

  /** The progress sync of a binding (M4 L46, EF-LEC-014): a reserved action, so the fresh-session
   * prompt may come first and the same call is replayed. The box shows the state the server
   * answered: a refusal puts it back where it was and says why. */
  async function setSync(dest: Destination, enabled: boolean, box: HTMLInputElement): Promise<void> {
    const call = async (): Promise<void> => {
      const { error } = await client.post({
        url: '/api/v1/destinations/{destination_id}/sync-progress',
        path: { destination_id: dest.id },
        headers: headers(),
        body: { enabled },
        throwOnError: false
      });
      if (error) {
        box.checked = Boolean(dest.sync_progress);
        if (askAgain(error, call)) return;
        const unsupported = problemOf(error).code === 'destination.sync_unsupported';
        say(t(unsupported ? 'destinations.sync.unsupported' : 'destinations.refused'), 'alert');
        return;
      }
      dest.sync_progress = enabled;
      box.checked = enabled;
      say(t(enabled ? 'destinations.sync.on' : 'destinations.sync.off'), 'status');
    };
    await call();
  }

  function startRemove(dest: Destination): void {
    removing = dest;
    removeMode = 'keep_files';
    removePreview = null;
    leftCount = 0;
    notice = null;
    job = null;
  }

  function cancelRemove(): void {
    removing = null;
    removePreview = null;
  }

  async function previewRemoval(): Promise<void> {
    if (!removing) return;
    const { data, error } = await client.post({
      url: '/api/v1/destinations/{destination_id}/removal-preview',
      path: { destination_id: removing.id },
      headers: headers(),
      body: { mode: removeMode },
      throwOnError: false
    });
    if (error) {
      if (!askAgain(error, previewRemoval)) say(t('destinations.refused'), 'alert');
      return;
    }
    removePreview = data as RemovalPreview;
  }

  async function confirmRemoval(typed: string): Promise<void> {
    if (!removing || !removePreview || busy) return;
    busy = true;
    try {
      const { data, error, response } = await client.post({
        url: '/api/v1/destinations/{destination_id}/removal',
        path: { destination_id: removing.id },
        headers: headers(),
        body: { mode: removeMode, typed },
        throwOnError: false
      });
      if (error) {
        if (!askAgain(error, () => confirmRemoval(typed))) {
          say(t('destinations.removal.refused'), 'alert');
        }
        return;
      }
      if (response?.status === 202) {
        startJob('removal', data as JobAccepted); // the files go in a job: nothing is done yet
        return;
      }
      const report = data as RemovalReport;
      cancelRemove();
      leftCount = report.left.length;
      if (report.complete) say(t('destinations.removal.done'), 'status');
      else say(t('destinations.removal.unfinished'), 'alert');
      await load();
    } finally {
      busy = false;
    }
  }

  async function startDetach(dest: Destination): Promise<void> {
    detaching = dest;
    detachPreview = null;
    job = null;
    detachPassword = '';
    discardHidden = false;
    discardJournal = false;
    keyGone = false;
    notice = null;
    const { data, error } = await client.post({
      url: '/api/v1/targets/{target_id}/detach-preview',
      path: { target_id: dest.target_id },
      headers: headers(),
      throwOnError: false
    });
    if (error) {
      if (!askAgain(error, () => startDetach(dest))) {
        detaching = null;
        say(t('destinations.refused'), 'alert');
      }
      return;
    }
    detachPreview = data as DetachPreview;
  }

  function cancelDetach(): void {
    detaching = null;
    detachPreview = null;
    detachPassword = '';
  }

  async function confirmDetach(typed: string): Promise<void> {
    if (!detaching || !detachPreview || busy) return;
    busy = true;
    try {
      const body: Record<string, unknown> = {
        typed,
        discard_hidden_data: discardHidden,
        discard_tombstone_journal: discardJournal
      };
      if (detachPassword !== '') body.credential = detachPassword;
      if (keyGone) body.key_gone = true;
      const { data, error } = await client.post({
        url: '/api/v1/targets/{target_id}/detach',
        path: { target_id: detaching.target_id },
        headers: headers(),
        body,
        throwOnError: false
      });
      if (error) {
        if (!askAgain(error, () => confirmDetach(typed))) {
          say(t('destinations.detach.refused'), 'alert');
        }
        return;
      }
      startJob('detach', data as JobAccepted);
    } finally {
      busy = false;
    }
  }

  /** The order was accepted: the form closes (and the password with it), the job is followed. */
  function startJob(kind: Job['kind'], accepted: JobAccepted): void {
    cancelRemove();
    cancelDetach();
    notice = null;
    leftCount = 0;
    job = { kind, url: accepted.status_url, status: 'queued', error: null };
  }

  /** « Recharger »: what the job says now, and the list again. */
  async function reload(): Promise<void> {
    if (!job || busy) return;
    busy = true;
    try {
      const { data, error } = await client.get({ url: job.url, throwOnError: false });
      if (!error && data) {
        const state = data as { status: string; error_code: string | null };
        job = { ...job, status: state.status, error: state.error_code };
      }
      await load();
    } finally {
      busy = false;
    }
  }

  const jobFailed = (j: Job): boolean => j.status === 'failed' || j.status === 'dead_letter';
  const jobRunning = (j: Job): boolean => ['queued', 'running'].includes(j.status);

  onMount(load);
</script>

<svelte:head>
  <title>{t('destinations.title')}</title>
</svelte:head>

<main class="form-screen">
  <h1>{t('destinations.title')}</h1>
  {#if adminOnly}
    <p role="alert">{notice?.text}</p>
  {:else}
    {#if destinations.length === 0}
      <p>{t('destinations.empty')}</p>
    {/if}
    <ul>
      {#each destinations as dest (dest.id)}
        <li>
          <span>{dest.shelf_name}</span>
          <span>{dest.root_subpath}</span>
          {#if dest.can_sync_progress}
            <label>
              <input
                type="checkbox"
                checked={Boolean(dest.sync_progress)}
                onchange={(event) => setSync(dest, event.currentTarget.checked, event.currentTarget)}
              />
              {t('destinations.sync.label')}
            </label>
          {/if}
          <button type="button" onclick={() => startRemove(dest)}>{t('destinations.remove')}</button>
          <button type="button" aria-describedby="scope-{dest.id}" onclick={() => startDetach(dest)}>
            {t('destinations.detach')}
          </button>
          <small id="scope-{dest.id}">{t('destinations.detach.scope', { names: concerned(dest) })}</small>
        </li>
      {/each}
    </ul>

    {#if removing}
      <section aria-labelledby="removal-title">
        <h2 id="removal-title">{t('destinations.remove')}</h2>
        <fieldset>
          <legend>{t('destinations.removal.mode')}</legend>
          <label>
            <input type="radio" bind:group={removeMode} value="keep_files" onchange={() => (removePreview = null)} />
            {t('destinations.removal.keep')}
          </label>
          <label>
            <input type="radio" bind:group={removeMode} value="delete_files" onchange={() => (removePreview = null)} />
            {t('destinations.removal.delete')}
          </label>
        </fieldset>
        <button type="button" onclick={previewRemoval}>{t('destinations.removal.preview_button')}</button>
        {#if removePreview}
          <ul>
            {#each removePreview.warnings as line (line.code)}
              <li>{warningText(line)}</li>
            {/each}
          </ul>
          <TypedConfirm
            phrase={removePreview.phrase}
            confirmLabel={t('destinations.confirm')}
            {busy}
            onconfirm={confirmRemoval}
          />
        {/if}
        <button type="button" onclick={cancelRemove}>{t('destinations.cancel')}</button>
      </section>
    {/if}

    {#if detaching}
      <section aria-labelledby="detach-title">
        <h2 id="detach-title">{t('destinations.detach')}</h2>
        <p>{t('destinations.detach.scope', { names: concerned(detaching) })}</p>
        {#if detachPreview}
          <ul>
            {#each detachPreview.warnings as line (line.code)}
              <li>{warningText(line)}</li>
            {/each}
          </ul>
          <label>
            {t('destinations.detach.password_label')}
            <input type="password" autocomplete="off" bind:value={detachPassword} />
          </label>
          <label><input type="checkbox" bind:checked={discardHidden} /> {t('destinations.detach.discard_hidden')}</label>
          <label><input type="checkbox" bind:checked={discardJournal} /> {t('destinations.detach.discard_journal')}</label>
          <label><input type="checkbox" bind:checked={keyGone} /> {t('destinations.detach.key_gone')}</label>
          <TypedConfirm
            phrase={detachPreview.phrase}
            confirmLabel={t('destinations.confirm')}
            {busy}
            onconfirm={confirmDetach}
          />
        {/if}
        <button type="button" onclick={cancelDetach}>{t('destinations.cancel')}</button>
      </section>
    {/if}

    {#if job}
      <section aria-labelledby="job-title">
        <h2 id="job-title">{t('destinations.job.title')}</h2>
        {#if job.status === 'succeeded' || jobFailed(job)}
          <h3>{t('destinations.report.title')}</h3>
        {/if}
        {#if job.status === 'succeeded'}
          <p role="status">
            {job.kind === 'removal' ? t('destinations.removal.done') : t('destinations.detach.done')}
          </p>
          {#if job.kind === 'detach'}
            <p>{t('destinations.detach.left_in_place')} : {t('destinations.detach.stays')}</p>
          {/if}
        {:else if jobFailed(job)}
          {#if job.error === 'removal.incomplete'}
            <p role="alert">{t('destinations.removal.unfinished')}</p>
            <p>{t('destinations.detach.left_in_place')} : {t('destinations.job.removal_left')}</p>
          {:else if job.error === 'detach.incomplete'}
            <p role="alert">{t('destinations.detach.unfinished')}</p>
            <p>{t('destinations.job.detach_again')}</p>
          {:else}
            <p role="alert">{t('destinations.job.failed')}</p>
          {/if}
        {:else if job.status === 'paused'}
          <p role="status">{t('destinations.job.paused')}</p>
        {:else if job.status === 'cancelled'}
          <p role="status">{t('destinations.job.cancelled')}</p>
        {:else if jobRunning(job)}
          <p role="status">{t('destinations.job.maybe_running')}</p>
        {:else}
          <p role="status">{t('destinations.job.maybe_running')}</p>
        {/if}
        <button type="button" disabled={busy} onclick={reload}>{t('destinations.job.reload')}</button>
      </section>
    {/if}

    {#if notice}
      <p role={notice.kind}>{notice.text}</p>
    {/if}
    {#if leftCount > 0}
      <p>{t('destinations.removal.left', { count: leftCount })}</p>
    {/if}
  {/if}

  {#if reauthMethods}
    <ReauthPrompt
      methods={reauthMethods}
      ondone={async () => {
        reauthMethods = null;
        const replay = retry;
        retry = null;
        await replay?.();
      }}
      oncancel={() => {
        reauthMethods = null;
        retry = null;
      }}
    />
  {/if}
</main>

<style>
  main { max-width: 60rem; margin: 0 auto; padding: 2rem 1rem; }
  ul { list-style: none; padding: 0; }
  li { margin-bottom: 1rem; }
  section { margin: 1rem 0; padding: 1rem; border: 1px solid #ccc; }
  label { display: block; margin: 0.25rem 0; }
</style>
