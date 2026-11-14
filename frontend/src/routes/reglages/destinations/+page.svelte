<script lang="ts">
  import { onMount } from 'svelte';
  import { client } from '#lib/api/client.gen.ts';
  import { t } from '#lib/i18n/index.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import TypedConfirm from '#lib/components/TypedConfirm.svelte';
  import { csrfToken } from '#lib/passkeys.ts';
  import { isForbidden, needsReauth, problemOf } from '#lib/triage.ts';

  type Destination = {
    id: string;
    shelf_id: string;
    shelf_name: string;
    target_id: string;
    root_subpath: string;
    transport: string;
    adopted: boolean;
    flagged_names: number;
    template: Record<string, string>;
  };
  type RemovalPreview = {
    destination_id: string;
    mode: string;
    phrase: string;
    warnings: { code: string; count: number }[];
  };
  type RemovalReport = {
    deleted: number;
    forgotten: number;
    cancelled: boolean;
    complete: boolean;
    left: { code: string; path: string }[];
  };
  type DetachPreview = {
    target_id: string;
    phrase: string;
    warnings: { code: string; count: number }[];
  };
  type DetachReport = {
    target_id: string;
    complete: boolean;
    hidden_removed: number;
    key_revoked: boolean;
    key_gone: boolean;
    forgotten: number;
    cancelled: boolean;
    settings_restored: boolean;
    settings: { exclude_patterns_restored: boolean; type_unchanged: boolean }[];
    leftovers: { code: string; count: number }[];
  };

  let destinations = $state<Destination[]>([]);
  let adminOnly = $state(false);
  let failed = $state(false);
  let message = $state('');
  let messageKind = $state<'status' | 'alert'>('status');

  // removal state
  let removing = $state<Destination | null>(null);
  let removeMode = $state<'keep_files' | 'delete_files'>('keep_files');
  let removePreview = $state<RemovalPreview | null>(null);
  let removeTyped = $state('');
  let removeCalling = $state(false);
  let removeError = $state('');

  // detach state
  let detachingTarget = $state<string | null>(null);
  let detachPreview = $state<DetachPreview | null>(null);
  let detachPassword = $state('');
  let discardHidden = $state(false);
  let discardJournal = $state(false);
  let detachTyped = $state('');
  let detachCalling = $state(false);
  let detachError = $state('');
  let detachReport = $state<DetachReport | null>(null);

  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  async function load(): Promise<void> {
    const { data, error } = await client.get({ url: '/api/v1/destinations', throwOnError: false });
    if (error) {
      if (isForbidden(error)) {
        adminOnly = true;
        message = t('screens.admin_only');
      } else {
        failed = true;
        message = t('destinations.error');
      }
      return;
    }
    destinations = (data as { items: Destination[] }).items ?? [];
  }

  function leftoverText(item: { code: string; count: number }): string {
    if (item.code === 'suwayomi_downloads') {
      return t('destinations.detach.leftover_suwayomi', { count: item.count });
    }
    if (item.code === 'kavita_accounts') {
      return t('destinations.detach.leftover_accounts', { count: item.count });
    }
    if (item.code === 'store') {
      return t('destinations.detach.leftover_store', { count: item.count });
    }
    return t('destinations.detach.leftover_other', { code: item.code, count: item.count });
  }

  async function startRemove(dest: Destination): Promise<void> {
    removing = dest;
    removeMode = 'keep_files';
    removePreview = null;
    removeTyped = '';
    removeError = '';
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
      if (needsReauth(error)) {
        retry = () => previewRemoval();
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else {
        removeError = t('destinations.refused');
      }
      return;
    }
    removePreview = data as RemovalPreview;
  }

  async function confirmRemoval(): Promise<void> {
    if (!removing || !removePreview) return;
    removeCalling = true;
    removeError = '';
    try {
      const { data, error } = await client.post({
        url: '/api/v1/destinations/{destination_id}/removal',
        path: { destination_id: removing.id },
        headers: headers(),
        body: { mode: removeMode, typed: removeTyped },
        throwOnError: false
      });
      if (error) {
        if (needsReauth(error)) {
          retry = () => confirmRemoval();
          reauthMethods = problemOf(error).reauth_methods ?? ['password'];
          return;
        }
        if (problemOf(error).type?.endsWith('removal.refused') || problemOf(error).code === 'removal.refused') {
          failed = true;
          message = t('destinations.removal.refused');
          messageKind = 'alert';
        } else {
          failed = true;
          message = t('destinations.removal.refused');
          messageKind = 'alert';
        }
        return;
      }
      const report = data as RemovalReport;
      removing = null;
      removePreview = null;
      removeTyped = '';
      if (report.complete) {
        failed = false;
        message = t('destinations.removal.done');
        messageKind = 'status';
      } else {
        failed = true;
        message = t('destinations.removal.unfinished');
        messageKind = 'alert';
      }
      await load();
    } finally {
      removeCalling = false;
    }
  }

  async function startDetach(dest: Destination): Promise<void> {
    detachingTarget = dest.target_id;
    detachPreview = null;
    detachPassword = '';
    discardHidden = false;
    discardJournal = false;
    detachTyped = '';
    detachError = '';
    detachReport = null;
    const { data, error } = await client.post({
      url: '/api/v1/targets/{target_id}/detach-preview',
      path: { target_id: dest.target_id },
      headers: headers(),
      throwOnError: false
    });
    if (error) {
      if (needsReauth(error)) {
        retry = () => startDetach(dest);
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else {
        detachError = t('destinations.refused');
      }
      return;
    }
    detachPreview = data as DetachPreview;
  }

  async function confirmDetach(): Promise<void> {
    if (!detachingTarget || !detachPreview) return;
    detachCalling = true;
    detachError = '';
    try {
      const body: Record<string, unknown> = {
        typed: detachTyped,
        discard_hidden_data: discardHidden,
        discard_tombstone_journal: discardJournal
      };
      if (detachPassword.trim() !== '') body.credential = detachPassword;
      const { data, error } = await client.post({
        url: '/api/v1/targets/{target_id}/detach',
        path: { target_id: detachingTarget },
        headers: headers(),
        body,
        throwOnError: false
      });
      if (error) {
        if (needsReauth(error)) {
          retry = () => confirmDetach();
          reauthMethods = problemOf(error).reauth_methods ?? ['password'];
          return;
        }
        if (problemOf(error).type?.endsWith('detach.refused') || problemOf(error).code === 'detach.refused') {
          failed = true;
          message = t('destinations.detach.refused');
          messageKind = 'alert';
        } else {
          failed = true;
          message = t('destinations.detach.refused');
          messageKind = 'alert';
        }
        detachReport = null;
        return;
      }
      detachReport = data as DetachReport;
      detachPassword = '';
    } finally {
      detachCalling = false;
    }
  }

  function cancelRemove(): void {
    removing = null;
    removePreview = null;
    removeTyped = '';
  }

  function cancelDetach(): void {
    detachingTarget = null;
    detachPreview = null;
    detachReport = null;
    detachTyped = '';
    detachPassword = '';
  }

  function removalWarningText(w: { code: string; count: number }): string {
    if (w.code === 'removal:files_deleted') {
      return t('destinations.removal.files_deleted', { count: w.count });
    }
    return `${w.code} ${w.count}`;
  }

  function detachWarningText(w: { code: string; count: number }): string {
    if (w.code === 'detach:hidden_erased') {
      return t('destinations.detach.hidden_erased', { count: w.count });
    }
    return `${w.code} ${w.count}`;
  }

  onMount(load);
</script>

<svelte:head>
  <title>{t('destinations.title')}</title>
</svelte:head>

<main>
  <h1>{t('destinations.title')}</h1>
  {#if adminOnly}
    <p role="alert">{message}</p>
  {:else}
    {#if destinations.length === 0 && !adminOnly}
      <p>{t('destinations.empty')}</p>
    {/if}
    <ul>
      {#each destinations as dest (dest.id)}
        <li>
          <span>{dest.shelf_name}</span>
          <span>{dest.root_subpath}</span>
          <button type="button" onclick={() => startRemove(dest)}>{t('destinations.remove')}</button>
          <button type="button" onclick={() => startDetach(dest)}>{t('destinations.detach')}</button>
        </li>
      {/each}
    </ul>

    {#if removing}
      <section>
        <h2>{t('destinations.remove')}</h2>
        <label><input type="radio" bind:group={removeMode} value="keep_files" /> {t('destinations.removal.keep')}</label>
        <label><input type="radio" bind:group={removeMode} value="delete_files" /> {t('destinations.removal.delete')}</label>
        <button type="button" onclick={previewRemoval}>{t('destinations.removal.preview_button')}</button>
        {#if removePreview}
          {#each removePreview.warnings as w (w.code)}
            <p>{removalWarningText(w)}</p>
          {/each}
          <p>{removePreview.phrase}</p>
          <TypedConfirm
            phrase={removePreview.phrase}
            confirmLabel={t('destinations.confirm')}
            onconfirm={() => { removeTyped = removePreview!.phrase; void confirmRemoval(); }}
          />
          <!-- typed binding handled below via input? TypedConfirm manages its own input -->
        {/if}
        <button type="button" onclick={cancelRemove}>{t('destinations.cancel')}</button>
      </section>
    {/if}

    {#if removing && removePreview}
      <!-- ensure typed value captured from TypedConfirm? TypedConfirm calls onconfirm(typed) -->
    {/if}

    {#if detachingTarget}
      <section>
        <h2>{t('destinations.detach')}</h2>
        {#if detachPreview}
          <p>{detachWarningText(detachPreview.warnings[0])}</p>
          <p>{detachPreview.phrase}</p>
          <label>
            {t('destinations.detach.password_label')}
            <input type="password" bind:value={detachPassword} />
          </label>
          <label><input type="checkbox" bind:checked={discardHidden} /> {t('destinations.detach.discard_hidden')}</label>
          <label><input type="checkbox" bind:checked={discardJournal} /> {t('destinations.detach.discard_journal')}</label>
          <TypedConfirm
            phrase={detachPreview.phrase}
            confirmLabel={t('destinations.confirm')}
            onconfirm={(typed) => { detachTyped = typed; void confirmDetach(); }}
          />
        {/if}
        <button type="button" onclick={cancelDetach}>{t('destinations.cancel')}</button>
      </section>
    {/if}

    {#if detachReport}
      <section role="region" aria-label={t('destinations.report.title')}>
        <h2>{t('destinations.report.title')}</h2>
        {#if detachReport.complete}
          <p>{t('destinations.detach.done')}</p>
        {:else}
          <p role="alert">{t('destinations.detach.unfinished')}</p>
        {/if}
        {#if detachReport.settings_restored}
          <p>{t('destinations.detach.settings_restored')}</p>
        {:else}
          <p>{t('destinations.detach.settings_not_restored')}</p>
        {/if}
        {#if detachReport.key_revoked}
          <p>{t('destinations.detach.key_revoked')}</p>
        {:else}
          <p>{t('destinations.detach.key_not_revoked')}</p>
        {/if}
        <p>{t('destinations.detach.hidden_removed', { count: detachReport.hidden_removed })}</p>
        {#if detachReport.leftovers.length > 0}
          <ul role="list" aria-label={t('destinations.detach.left_in_place')}>
            {#each detachReport.leftovers as item (item.code)}
              <li>{leftoverText(item)}</li>
            {/each}
          </ul>
        {/if}
      </section>
    {/if}

    {#if message && !adminOnly}
      <p role={messageKind}>{message}</p>
    {/if}
  {/if}

  {#if reauthMethods}
    <ReauthPrompt
      methods={reauthMethods}
      ondone={async () => {
        reauthMethods = null;
        const r = retry;
        retry = null;
        await r?.();
      }}
      oncancel={() => { reauthMethods = null; retry = null; }}
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