<script lang="ts">
  // The adoption of a library Kavita already has (EF-EXP-045, EF-EXP-060; M4 L36). A preview first:
  // nothing is written until it is confirmed on exactly what was shown. Each archive of the library
  // comes from outside: its first page is read by the decode worker and a file whose page or
  // archive was refused is listed "to check". It is adopted as it is and never touched. All text
  // from the server (paths, codes) is shown as text, never as markup; an admin session only.
  import { onMount } from 'svelte';
  import { wizardAdoptionConfirm, wizardState } from '#lib/api/index.ts';
  import { previewAdoption } from '#lib/adoption.ts';
  import type { WizardDestinationOut, WizardPreviewOut } from '#lib/api/types.gen.ts';
  import ProblemAlert from '#lib/ui/molecules/ProblemAlert.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { hasMessage, t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, needsSession, problemOf } from '#lib/triage.ts';

  let destinations = $state<WizardDestinationOut[]>([]);
  let destination = $state('');
  let loaded = $state(false);
  let preview = $state<WizardPreviewOut | null>(null);
  let message = $state('');
  let problem = $state<ProblemView | null>(null);
  let signedOut = $state(false);
  let busy = $state(false);
  let waiting = $state(false); // the preview is a task: it can take minutes
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const reason = (code: string): string => {
    const key = `adoption.reason.${code}`;
    return hasMessage(key) ? t(key) : t('adoption.reason.other');
  };

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

  const look = () =>
    guarded(async () => {
      message = '';
      waiting = true;
      try {
        preview = await previewAdoption({ destination_id: destination }, headers());
      } finally {
        waiting = false;
      }
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
      message = t('adoption.adopted', { count: data.adopted_files });
      preview = null;
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
  <title>{t('adoption.title')}</title>
</svelte:head>

<main class="page page--form">
  <PageHead
    title={t('adoption.title')}
    crumbs={[{ label: t('settings.title'), href: '/reglages' }, { label: t('adoption.title') }]}
  />

  {#if signedOut}
    <Alert tone="warning" title={t('admin.wizard.signed_out')} live="assertive">
      {#snippet actions()}
        <Button href="/login" data-sveltekit-reload>{t('admin.wizard.login')}</Button>
      {/snippet}
    </Alert>
  {:else}
    <div class="stack">
      {#if reauthMethods}
        <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
      {/if}
      {#if problem}
        <ProblemAlert {problem} />
      {/if}
      {#if message}
        <Alert tone="success" title={message} live="polite" />
      {/if}

      {#if !loaded}
        <div aria-busy="true">
          <p class="visually-hidden" role="status">{t('adoption.loading')}</p>
          <Skeleton shape="block" /><Skeleton short />
        </div>
      {:else if destinations.length === 0}
        <EmptyState title={t('adoption.no_destination')} art={false} />
      {:else}
        <p class="lede">{t('adoption.explain')}</p>
        <div class="panel step">
          {#if destinations.length > 1}
            <Field label={t('adoption.destination')}>
              {#snippet children(control)}
                <Select
                  {...control}
                  bind:value={destination}
                  onchange={() => (preview = null)}
                  options={destinations.map((item) => ({ value: item.id, label: item.id }))}
                />
              {/snippet}
            </Field>
          {/if}
          <div><Button variant={preview ? 'secondary' : 'primary'} icon="eye" loading={busy && !preview} onclick={look}>{t('adoption.preview')}</Button></div>
          {#if waiting}
            <p role="status" class="muted">{t('adoption.waiting')}</p>
          {/if}
        </div>

        {#if preview}
          <section class="panel step" aria-label={t('adoption.preview')}>
            <p class="figures">{t('adoption.preview_result', { series: preview.series, files: preview.files })}</p>
            <p class="muted">{t('adoption.not_probed')}</p>
            {#if preview.child_library}
              <Alert tone="info" title={t('adoption.child_library')} live="polite" />
            {/if}
            {#if preview.to_check.length > 0}
              <section aria-labelledby="to-check-title">
                <div class="section-head">
                  <h2 id="to-check-title">{t('adoption.to_check_title', { count: preview.to_check.length })}</h2>
                </div>
                <p>{t('adoption.to_check_explain')}</p>
                <ul class="entries">
                  {#each preview.to_check as item (item.path)}
                    <li class="check"><code>{item.path}</code> : {reason(item.reason)}</li>
                  {/each}
                </ul>
              </section>
            {/if}
            <div><Button variant="primary" loading={busy} onclick={adopt}>{t('adoption.adopt')}</Button></div>
          </section>
        {/if}
      {/if}
    </div>
  {/if}
</main>

<style>
  .step {
    display: grid;
    gap: var(--space-4);
    justify-items: start;
  }
  .step > :global(*) {
    max-width: 100%;
  }
  .figures {
    margin: 0;
    font: 500 var(--step-2) / var(--leading-tight) var(--font-text);
    font-variant-numeric: tabular-nums;
  }
  .check {
    padding: var(--space-2) 0;
    overflow-wrap: anywhere;
  }
  .check code {
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: var(--step--1);
  }
</style>
