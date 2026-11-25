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
  import Button from '#lib/ui/atoms/Button.svelte';
  import Checkbox from '#lib/ui/atoms/Checkbox.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Radio from '#lib/ui/atoms/Radio.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Switch from '#lib/ui/atoms/Switch.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

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
  let loaded = $state(false);
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
    loaded = true;
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

  onMount(load);
</script>

<svelte:head>
  <title>{t('destinations.title')}</title>
</svelte:head>

<main class="page">
  <PageHead
    title={t('destinations.title')}
    crumbs={[{ label: t('settings.title'), href: '/reglages' }, { label: t('destinations.title') }]}
    lede={t('destinations.lede')}
  />
  {#if adminOnly}
    <Alert tone="danger" title={notice?.text ?? ''} live="assertive" />
  {:else if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('destinations.loading')}</p>
      <Skeleton shape="block" /><Skeleton /><Skeleton short />
    </div>
  {:else}
    {#if destinations.length === 0}
      <EmptyState title={t('destinations.empty')} />
    {:else}
      <ul class="entries destinations">
        {#each destinations as dest (dest.id)}
          <li class="dest">
            <div class="dest__name">
              <span class="dest__shelf">{dest.shelf_name}</span>
              <code class="dest__path">{dest.root_subpath}</code>
            </div>
            {#if dest.can_sync_progress}
              <Switch
                label={t('destinations.sync.label')}
                checked={Boolean(dest.sync_progress)}
                onchange={(event) => setSync(dest, event.currentTarget.checked, event.currentTarget)}
              />
            {/if}
            <div class="dest__actions">
              <Button variant="danger-ghost" size="sm" onclick={() => startRemove(dest)}>{t('destinations.remove')}</Button>
              <Button size="sm" aria-describedby="scope-{dest.id}" onclick={() => startDetach(dest)}>
                {t('destinations.detach')}
              </Button>
            </div>
            <small class="dest__scope hint" id="scope-{dest.id}">{t('destinations.detach.scope', { names: concerned(dest) })}</small>
          </li>
        {/each}
      </ul>
    {/if}

    {#if removing}
      <section class="panel step" aria-labelledby="removal-title">
        <h2 id="removal-title">{t('destinations.remove')}</h2>
        <fieldset class="choices">
          <legend>{t('destinations.removal.mode')}</legend>
          <Radio bind:group={removeMode} value="keep_files" label={t('destinations.removal.keep')} onchange={() => (removePreview = null)} />
          <Radio bind:group={removeMode} value="delete_files" label={t('destinations.removal.delete')} onchange={() => (removePreview = null)} />
        </fieldset>
        <div class="btn-row">
          <Button variant="primary" onclick={previewRemoval}>{t('destinations.removal.preview_button')}</Button>
          <Button variant="ghost" onclick={cancelRemove}>{t('destinations.cancel')}</Button>
        </div>
        {#if removePreview}
          <Alert tone="warning" title={t('destinations.preview.title')}>
            <ul class="warnings">
              {#each removePreview.warnings as line (line.code)}
                <li>{warningText(line)}</li>
              {/each}
            </ul>
          </Alert>
          <TypedConfirm
            phrase={removePreview.phrase}
            confirmLabel={t('destinations.confirm')}
            {busy}
            onconfirm={confirmRemoval}
          />
        {/if}
      </section>
    {/if}

    {#if detaching}
      <section class="panel step" aria-labelledby="detach-title">
        <h2 id="detach-title">{t('destinations.detach')}</h2>
        <p>{t('destinations.detach.scope', { names: concerned(detaching) })}</p>
        {#if detachPreview}
          <Alert tone="warning" title={t('destinations.preview.title')}>
            <ul class="warnings">
              {#each detachPreview.warnings as line (line.code)}
                <li>{warningText(line)}</li>
              {/each}
            </ul>
          </Alert>
          <Field label={t('destinations.detach.password_label')}>
            {#snippet children(control)}
              <Input {...control} type="password" autocomplete="off" bind:value={detachPassword} />
            {/snippet}
          </Field>
          <div class="choices">
            <Checkbox bind:checked={discardHidden} label={t('destinations.detach.discard_hidden')} />
            <Checkbox bind:checked={discardJournal} label={t('destinations.detach.discard_journal')} />
            <Checkbox bind:checked={keyGone} label={t('destinations.detach.key_gone')} />
          </div>
          <TypedConfirm
            phrase={detachPreview.phrase}
            confirmLabel={t('destinations.confirm')}
            {busy}
            onconfirm={confirmDetach}
          />
        {/if}
        <Button variant="ghost" onclick={cancelDetach}>{t('destinations.cancel')}</Button>
      </section>
    {/if}

    {#if job}
      <section class="panel step" aria-labelledby="job-title">
        <h2 id="job-title">{t('destinations.job.title')}</h2>
        {#if job.status === 'succeeded' || jobFailed(job)}
          <h3>{t('destinations.report.title')}</h3>
        {/if}
        {#if job.status === 'succeeded'}
          <Alert tone="success" live="polite" title={job.kind === 'removal' ? t('destinations.removal.done') : t('destinations.detach.done')} />
          {#if job.kind === 'detach'}
            <p>{t('destinations.detach.left_in_place')} : {t('destinations.detach.stays')}</p>
          {/if}
        {:else if jobFailed(job)}
          {#if job.error === 'removal.incomplete'}
            <Alert tone="danger" live="assertive" title={t('destinations.removal.unfinished')} />
            <p>{t('destinations.detach.left_in_place')} : {t('destinations.job.removal_left')}</p>
          {:else if job.error === 'detach.incomplete'}
            <Alert tone="danger" live="assertive" title={t('destinations.detach.unfinished')} />
            <p>{t('destinations.job.detach_again')}</p>
          {:else}
            <Alert tone="danger" live="assertive" title={t('destinations.job.failed')} />
          {/if}
        {:else if job.status === 'paused'}
          <Alert tone="info" live="polite" title={t('destinations.job.paused')} />
        {:else if job.status === 'cancelled'}
          <Alert tone="info" live="polite" title={t('destinations.job.cancelled')} />
        {:else}
          <Alert tone="info" live="polite" title={t('destinations.job.maybe_running')} />
        {/if}
        <div><Button icon="refresh" disabled={busy} onclick={reload}>{t('destinations.job.reload')}</Button></div>
      </section>
    {/if}

    {#if notice}
      <Alert tone={notice.kind === 'alert' ? 'danger' : 'success'} live={notice.kind === 'alert' ? 'assertive' : 'polite'} title={notice.text} />
    {/if}
    {#if leftCount > 0}
      <p class="muted">{t('destinations.removal.left', { count: leftCount })}</p>
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
  .dest {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    align-items: center;
    gap: var(--space-2) var(--space-5);
    padding: var(--space-4) 0;
  }
  .dest__name {
    display: grid;
    min-width: 0;
  }
  .dest__shelf {
    font-weight: 600;
  }
  .dest__path {
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: var(--step--1);
    color: var(--color-muted);
    overflow-wrap: anywhere;
  }
  .dest__actions {
    display: flex;
    gap: var(--space-2);
  }
  .dest__scope {
    grid-column: 1 / -1;
  }
  .step {
    display: grid;
    gap: var(--space-4);
    justify-items: start;
    margin-top: var(--space-6);
  }
  .step > :global(*) {
    max-width: 100%;
  }
  .step h2,
  .step h3 {
    margin: 0;
    font-size: var(--step-1);
  }
  .choices {
    display: grid;
    gap: var(--space-2);
    margin: 0;
    padding: 0;
    border: 0;
  }
  .choices legend {
    margin-bottom: var(--space-2);
    font-weight: 600;
  }
  .warnings {
    margin: 0;
    padding-inline-start: var(--space-5);
  }
  @media (max-width: 40rem) {
    .dest {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
