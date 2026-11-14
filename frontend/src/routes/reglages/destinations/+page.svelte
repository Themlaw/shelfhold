<script lang="ts">
  // The destinations screen (EF-EXP-031): remove a destination or detach Kavita, each behind a
  // preview that says what would be lost, a phrase the server gives and the admin types, and the
  // fresh-session prompt. The server is the gate: it checks the phrase, the session and the role
  // again; nothing here decides for it.
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
  };
  type Line = { code: string; count: number };
  type RemovalPreview = { phrase: string; warnings: Line[] };
  type RemovalReport = { complete: boolean; left: { code: string }[] };
  type DetachPreview = { phrase: string; warnings: Line[] };
  type DetachReport = {
    complete: boolean;
    key_revoked: boolean;
    hidden_removed: number;
    settings_restored: boolean;
    leftovers: Line[];
  };

  const WARNINGS: Record<string, MessageKey> = {
    'removal:files_kept': 'destinations.warn.removal_files_kept',
    'removal:manifest_forgotten': 'destinations.warn.removal_manifest_forgotten',
    'removal:files_deleted': 'destinations.removal.files_deleted',
    'removal:past_the_bin': 'destinations.warn.removal_past_the_bin',
    'removal:adopted_kept': 'destinations.warn.removal_adopted_kept',
    'detach:files_kept': 'destinations.warn.detach_files_kept',
    'detach:hidden_erased': 'destinations.detach.hidden_erased',
    'detach:key_revoked': 'destinations.warn.detach_key_revoked',
    'detach:manifest_forgotten': 'destinations.warn.detach_manifest_forgotten',
    'detach:hidden_data_present': 'destinations.warn.detach_hidden_data_present',
    'detach:root_shared': 'destinations.warn.detach_root_shared',
    'detach:follows_active': 'destinations.warn.detach_follows_active',
    'detach:tombstone_journal_erased': 'destinations.warn.detach_tombstone_journal_erased'
  };
  const LEFTOVERS: Record<string, MessageKey> = {
    suwayomi_downloads: 'destinations.detach.leftover_suwayomi',
    kavita_accounts: 'destinations.detach.leftover_accounts',
    store: 'destinations.detach.leftover_store',
    root_shared: 'destinations.detach.leftover_root_shared',
    hidden_not_erased: 'destinations.detach.leftover_hidden_not_erased',
    settings_not_restored: 'destinations.detach.leftover_settings_not_restored',
    key_not_revoked: 'destinations.detach.leftover_key_not_revoked',
    erasing_name_taken: 'destinations.detach.leftover_erasing_name_taken',
    instance_changed: 'destinations.detach.leftover_instance_changed',
    key_gone: 'destinations.detach.leftover_key_gone',
    key_id_reused: 'destinations.detach.leftover_key_id_reused',
    hidden_pattern_kept: 'destinations.detach.leftover_hidden_pattern_kept',
    library_gone: 'destinations.detach.leftover_library_gone',
    tombstone_journal_erased: 'destinations.detach.leftover_tombstone_journal_erased',
    library_type_changed: 'destinations.detach.leftover_library_type_changed'
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
  let detachReport = $state<DetachReport | null>(null);

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
  const leftoverText = (line: Line): string => {
    const key = LEFTOVERS[line.code];
    return key ? t(key, { count: line.count }) : t('destinations.detach.leftover_other', line);
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

  function startRemove(dest: Destination): void {
    removing = dest;
    removeMode = 'keep_files';
    removePreview = null;
    leftCount = 0;
    notice = null;
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
      const { data, error } = await client.post({
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
    detachReport = null;
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
      detachReport = data as DetachReport;
      cancelDetach();
      await load();
    } finally {
      busy = false;
    }
  }

  onMount(load);
</script>

<svelte:head>
  <title>{t('destinations.title')}</title>
</svelte:head>

<main>
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

    {#if detachReport}
      <section aria-labelledby="report-title">
        <h2 id="report-title">{t('destinations.report.title')}</h2>
        {#if detachReport.complete}
          <p>{t('destinations.detach.done')}</p>
        {:else}
          <p role="alert">{t('destinations.detach.unfinished')}</p>
        {/if}
        <p>{detachReport.settings_restored ? t('destinations.detach.settings_restored') : t('destinations.detach.settings_not_restored')}</p>
        <p>{detachReport.key_revoked ? t('destinations.detach.key_revoked') : t('destinations.detach.key_not_revoked')}</p>
        <p>{t('destinations.detach.hidden_removed', { count: detachReport.hidden_removed })}</p>
        {#if detachReport.leftovers.length > 0}
          <ul aria-label={t('destinations.detach.left_in_place')}>
            {#each detachReport.leftovers as line (line.code)}
              <li>{leftoverText(line)}</li>
            {/each}
          </ul>
        {/if}
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
