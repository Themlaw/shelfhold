<script lang="ts">
  // Wizard screen 3: the shared folder (EF-UX-002) and the library (EF-UX-001). Shelfhold drops a
  // probe in the folder, Kavita is asked to read it, and when it cannot, the exact compose block
  // is shown. Then: adopt the library Kavita already has (a preview first, nothing is written
  // until it is confirmed on what was shown) or make a new one. All text from the server is shown
  // as text, never as markup.
  import { onDestroy, onMount } from 'svelte';
  import {
    wizardAdoptionConfirm,
    wizardNewLibrary,
    wizardProbe,
    wizardShelf,
    wizardState
  } from '#lib/api/index.ts';
  import type {
    WizardDestinationOut,
    WizardPreviewOut,
    WizardProbeOut
  } from '#lib/api/types.gen.ts';
  import { previewAdoption } from '#lib/adoption.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import WizardPage from '#lib/components/WizardPage.svelte';
  import WizardSignedOut from '#lib/components/WizardSignedOut.svelte';
  import { t } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, needsSession, problemOf } from '#lib/triage.ts';
  import { advanceFrom, SHELF_FOLDER } from '#lib/wizard.ts';

  let destinations = $state<WizardDestinationOut[]>([]);
  let targetName = $state<string | null>(null);
  let shelfName = $state('Livres');
  let shelfFolder = $state('livres');
  let shelfAge = $state('18+');
  let cancelAdvance = () => {};
  onDestroy(() => cancelAdvance());
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
      targetName = data.targets[0]?.name ?? null;
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
      preview = await previewAdoption(
        { destination_id: destination, download_dir: downloadDir.trim() || null },
        headers()
      );
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

  // The shelf linked to Kavita: the wizard's door to `shelfhold shelf create`, behind the same
  // guards (a fresh admin session, the age ceiling, the audit of who reads the Kavita library).
  const makeShelf = () =>
    guarded(async () => {
      message = '';
      await wizardShelf({
        headers: headers(),
        body: {
          name: shelfName.trim(),
          target: targetName ?? 'kavita',
          max_age: shelfAge,
          subpath: shelfFolder.trim()
        },
        throwOnError: true
      });
      message = t('admin.wizard.dossier.shelf_created');
      const { data } = await wizardState({ throwOnError: true });
      destinations = data.destinations;
      destination = destinations[0]?.id ?? '';
      cancelAdvance = advanceFrom('dossier'); // nothing else is asked: the wizard goes on
    });

  const shelfReady = $derived(shelfName.trim() !== '' && SHELF_FOLDER.test(shelfFolder.trim()));

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

<WizardPage current="dossier" title={t('admin.wizard.dossier.title')}>
  {#if signedOut}
    <WizardSignedOut />
  {:else}
    {#if reauthMethods}
      <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
    {/if}
    {#if problem}
      <ProblemCard {problem} />
    {/if}
    {#if message}
      <Alert tone="success" title={message} live="polite" />
    {/if}

    {#if !loaded}
      <div aria-busy="true">
        <p class="visually-hidden" role="status">{t('admin.wizard.loading')}</p>
        <Skeleton /><Skeleton short />
      </div>
    {:else if destinations.length === 0 && targetName === null}
      <Alert tone="warning" title={t('admin.wizard.dossier.no_target')}>
        {#snippet actions()}
          <Button href="/assistant/kavita" icon="left" size="sm">{t('admin.wizard.dossier.back_to_kavita')}</Button>
        {/snippet}
      </Alert>
    {:else if destinations.length === 0}
      <section class="panel step" aria-labelledby="shelf-title">
        <h3 id="shelf-title">{t('admin.wizard.dossier.shelf_title')}</h3>
        <p class="intro">{t('admin.wizard.dossier.shelf_explain')}</p>
        <Field label={t('admin.wizard.dossier.shelf_name')}>
          {#snippet children(control)}<Input {...control} type="text" bind:value={shelfName} maxlength={128} />{/snippet}
        </Field>
        <Field label={t('admin.wizard.dossier.shelf_folder')} hint={t('admin.wizard.dossier.shelf_folder_help')}>
          {#snippet children(control)}
            <Input {...control} type="text" bind:value={shelfFolder} maxlength={64} autocomplete="off" mono />
          {/snippet}
        </Field>
        <Field label={t('admin.wizard.dossier.shelf_age')} hint={t('admin.wizard.dossier.shelf_audit')}>
          {#snippet children(control)}
            <Select
              {...control}
              bind:value={shelfAge}
              options={[
                { value: '18+', label: t('admin.wizard.dossier.shelf_age_adult') },
                { value: '13+', label: t('admin.wizard.dossier.shelf_age_family') }
              ]}
            />
          {/snippet}
        </Field>
        <div class="btn-row">
          <Button variant="primary" disabled={!shelfReady} loading={busy} onclick={makeShelf}>
            {t('admin.wizard.dossier.shelf_create')}
          </Button>
        </div>
      </section>
    {:else}
      <p class="intro">{t('admin.wizard.dossier.explain')}</p>
      {#if destinations.length > 1}
        <Field label={t('admin.wizard.dossier.destination')}>
          {#snippet children(control)}
            <Select {...control} bind:value={destination} options={destinations.map((d) => ({ value: d.id, label: d.id }))} />
          {/snippet}
        </Field>
      {/if}

      <section class="panel step" aria-labelledby="probe-title">
        <h3 id="probe-title">{t('admin.wizard.dossier.check')}</h3>
        <div class="btn-row">
          <Button icon="pulse" disabled={busy} onclick={check}>{t('admin.wizard.dossier.check')}</Button>
        </div>
        {#if probe}
          {#if probe.ok && probe.kavita_read === true}
            <Alert tone="success" title={t('admin.wizard.dossier.ok')} live="polite" />
          {:else if probe.ok}
            <Alert tone="warning" title={t('admin.wizard.dossier.no_library')} live="polite" />
          {:else if !probe.write.ok}
            <Alert tone="danger" title={t('admin.wizard.dossier.write_failed')} live="assertive" />
          {:else}
            <Alert tone="danger" title={t('admin.wizard.dossier.kavita_failed')} live="assertive" />
          {/if}
          {#if probe.yaml_hint}
            <!-- a block that scrolls sideways must take the keyboard focus -->
            <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
            <pre class="yaml" tabindex="0"><code>{probe.yaml_hint}</code></pre>
          {/if}
        {/if}
      </section>

      <section class="panel step" aria-labelledby="adopt-title">
        <h3 id="adopt-title">{t('admin.wizard.dossier.adopt_title')}</h3>
        <Field label={t('admin.wizard.dossier.download_dir')}>
          {#snippet children(control)}<Input {...control} type="text" bind:value={downloadDir} mono />{/snippet}
        </Field>
        <div class="btn-row">
          <Button icon="search" disabled={busy} onclick={look}>{t('admin.wizard.dossier.preview')}</Button>
        </div>
        {#if preview}
          <div class="preview">
            <p>{t('admin.wizard.dossier.preview_result', { series: preview.series, files: preview.files })}</p>
            {#if preview.download_dir}
              <p>{t(`admin.wizard.dossier.verdict.${preview.download_dir.kind}`)}</p>
            {/if}
            <p>{t('admin.wizard.dossier.roots', { count: preview.roots.length })}</p>
          </div>
          <div class="btn-row">
            <Button variant="primary" disabled={busy} onclick={adopt}>{t('admin.wizard.dossier.adopt')}</Button>
          </div>
        {/if}
      </section>

      <section class="panel step" aria-labelledby="new-title">
        <h3 id="new-title">{t('admin.wizard.dossier.new_title')}</h3>
        <Field label={t('admin.wizard.dossier.new_name')}>
          {#snippet children(control)}<Input {...control} type="text" bind:value={libraryName} maxlength={128} />{/snippet}
        </Field>
        <div class="btn-row">
          <Button icon="plus" disabled={libraryName.trim() === ''} loading={busy} onclick={create}>
            {t('admin.wizard.dossier.new')}
          </Button>
        </div>
      </section>
    {/if}
  {/if}
</WizardPage>

<style>
  .step {
    display: grid;
    gap: var(--space-3);
  }
  .step h3 {
    margin: 0;
    font-size: var(--step-1);
  }
  .intro {
    margin: 0;
    max-width: var(--measure);
  }
  .preview p {
    margin: 0 0 var(--space-1);
  }
  .yaml {
    margin: 0;
    padding: var(--space-3) var(--space-4);
    overflow-x: auto;
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-radius: var(--radius-xs);
    background: var(--color-bg);
    font-size: var(--step--1);
    line-height: 1.5;
    tab-size: 2;
  }
</style>
