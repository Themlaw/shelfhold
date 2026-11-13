<script lang="ts">
  // Wizard screen 3: the shared folder (EF-UX-002) and the library (EF-UX-001). Shelfhold drops a
  // probe in the folder, Kavita is asked to read it, and when it cannot, the exact compose block
  // is shown. Then: adopt the library Kavita already has (a preview first, nothing is written
  // until it is confirmed on what was shown) or make a new one. All text from the server is shown
  // as text, never as markup.
  import { onMount } from 'svelte';
  import {
    wizardAdoptionConfirm,
    wizardAdoptionPreview,
    wizardNewLibrary,
    wizardProbe,
    wizardState
  } from '#lib/api/index.ts';
  import type {
    WizardDestinationOut,
    WizardPreviewOut,
    WizardProbeOut
  } from '#lib/api/types.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import WizardSteps from '#lib/components/WizardSteps.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, needsSession, problemOf } from '#lib/triage.ts';

  let destinations = $state<WizardDestinationOut[]>([]);
  let loaded = $state(false);
  let destination = $state('');
  let probe = $state<WizardProbeOut | null>(null);
  let preview = $state<WizardPreviewOut | null>(null);
  let downloadDir = $state('');
  let libraryName = $state('');
  let message = $state('');
  let problem = $state<ProblemView | null>(null);
  let signedOut = $state(false);
  let busy = $state(false);
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  /** Run an action; a stale password is asked in place and the action runs again after. */
  async function guarded(action: () => Promise<void>): Promise<void> {
    busy = true;
    problem = null;
    try {
      await action();
    } catch (error) {
      if (needsReauth(error)) {
        retry = action;
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else if (needsSession(error)) {
        signedOut = true;
      } else {
        problem = problemView(error);
      }
    } finally {
      busy = false;
    }
  }

  const load = () =>
    guarded(async () => {
      const { data } = await wizardState({ throwOnError: true });
      destinations = data.destinations;
      destination = destinations[0]?.id ?? '';
      loaded = true;
    });

  onMount(load);

  const check = () =>
    guarded(async () => {
      message = '';
      const { data } = await wizardProbe({
        headers: headers(),
        body: { destination_id: destination },
        throwOnError: true
      });
      probe = data;
    });

  const look = () =>
    guarded(async () => {
      message = '';
      const { data } = await wizardAdoptionPreview({
        headers: headers(),
        body: { destination_id: destination, download_dir: downloadDir.trim() || null },
        throwOnError: true
      });
      preview = data;
    });

  const adopt = () =>
    guarded(async () => {
      if (!preview) return;
      const { data } = await wizardAdoptionConfirm({
        headers: headers(),
        body: {
          destination_id: destination,
          fingerprint: preview.fingerprint,
          library_ref: preview.library_ref
        },
        throwOnError: true
      });
      message = t('admin.wizard.dossier.adopted', { count: data.adopted_files });
      preview = null;
    });

  const create = () =>
    guarded(async () => {
      await wizardNewLibrary({
        headers: headers(),
        body: { destination_id: destination, name: libraryName.trim() },
        throwOnError: true
      });
      message = t('admin.wizard.dossier.created');
      libraryName = '';
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
</script>

<svelte:head>
  <title>{t('admin.wizard.dossier.title')}</title>
</svelte:head>

<main>
  <h1>{t('admin.wizard.title')}</h1>
  <WizardSteps current="dossier" />
  <h2>{t('admin.wizard.dossier.title')}</h2>

  {#if signedOut}
    <p role="alert">{t('admin.wizard.signed_out')}</p>
    <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
    <p><a href="/login">{t('admin.wizard.login')}</a></p>
  {:else}
    {#if reauthMethods}
      <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
    {/if}
    {#if problem}
      <ProblemCard {problem} />
    {/if}
    {#if message}
      <p role="status">{message}</p>
    {/if}

    {#if loaded && destinations.length === 0}
      <p>{t('admin.wizard.dossier.no_destination')}</p>
    {:else if loaded}
      <p>{t('admin.wizard.dossier.explain')}</p>
      {#if destinations.length > 1}
        <label>
          {t('admin.wizard.dossier.destination')}
          <select bind:value={destination}>
            {#each destinations as d (d.id)}
              <option value={d.id}>{d.id}</option>
            {/each}
          </select>
        </label>
      {/if}

      <section aria-labelledby="probe-title">
        <h3 id="probe-title">{t('admin.wizard.dossier.check')}</h3>
        <button type="button" disabled={busy} onclick={check}>{t('admin.wizard.dossier.check')}</button>
        {#if probe}
          {#if probe.ok && probe.kavita_read === true}
            <p role="status">{t('admin.wizard.dossier.ok')}</p>
          {:else if probe.ok}
            <p role="status">{t('admin.wizard.dossier.no_library')}</p>
          {:else if !probe.write.ok}
            <p role="alert">{t('admin.wizard.dossier.write_failed')}</p>
          {:else}
            <p role="alert">{t('admin.wizard.dossier.kavita_failed')}</p>
          {/if}
          {#if probe.yaml_hint}
            <pre class="yaml"><code>{probe.yaml_hint}</code></pre>
          {/if}
        {/if}
      </section>

      <section aria-labelledby="adopt-title">
        <h3 id="adopt-title">{t('admin.wizard.dossier.adopt_title')}</h3>
        <label>
          {t('admin.wizard.dossier.download_dir')}
          <input type="text" bind:value={downloadDir} />
        </label>
        <button type="button" disabled={busy} onclick={look}>{t('admin.wizard.dossier.preview')}</button>
        {#if preview}
          <p>{t('admin.wizard.dossier.preview_result', { series: preview.series, files: preview.files })}</p>
          {#if preview.download_dir}
            <p>{t(`admin.wizard.dossier.verdict.${preview.download_dir.kind}`)}</p>
          {/if}
          <p>{t('admin.wizard.dossier.roots', { count: preview.roots.length })}</p>
          <button type="button" disabled={busy} onclick={adopt}>{t('admin.wizard.dossier.adopt')}</button>
        {/if}
      </section>

      <section aria-labelledby="new-title">
        <h3 id="new-title">{t('admin.wizard.dossier.new_title')}</h3>
        <label>
          {t('admin.wizard.dossier.new_name')}
          <input type="text" bind:value={libraryName} maxlength="128" />
        </label>
        <button type="button" disabled={busy || libraryName.trim() === ''} onclick={create}>
          {t('admin.wizard.dossier.new')}
        </button>
      </section>
    {/if}
  {/if}
</main>
