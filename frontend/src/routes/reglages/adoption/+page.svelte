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
  import ProblemCard from '#lib/components/ProblemCard.svelte';
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

<main>
  <h1>{t('adoption.title')}</h1>

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
      <p>{t('adoption.no_destination')}</p>
    {:else if loaded}
      <p>{t('adoption.explain')}</p>
      {#if destinations.length > 1}
        <label>
          {t('adoption.destination')}
          <select bind:value={destination} onchange={() => (preview = null)}>
            {#each destinations as item (item.id)}
              <option value={item.id}>{item.id}</option>
            {/each}
          </select>
        </label>
      {/if}
      <button type="button" disabled={busy} onclick={look}>{t('adoption.preview')}</button>
      {#if waiting}
        <p role="status">{t('adoption.waiting')}</p>
      {/if}

      {#if preview}
        <p>{t('adoption.preview_result', { series: preview.series, files: preview.files })}</p>
        <p>{t('adoption.not_probed')}</p>
        {#if preview.child_library}
          <p role="status">{t('adoption.child_library')}</p>
        {/if}
        {#if preview.to_check.length > 0}
          <section aria-labelledby="to-check-title">
            <h2 id="to-check-title">
              {t('adoption.to_check_title', { count: preview.to_check.length })}
            </h2>
            <p>{t('adoption.to_check_explain')}</p>
            <ul>
              {#each preview.to_check as item (item.path)}
                <li><code>{item.path}</code> : {reason(item.reason)}</li>
              {/each}
            </ul>
          </section>
        {/if}
        <button type="button" disabled={busy} onclick={adopt}>{t('adoption.adopt')}</button>
      {/if}
    {/if}
  {/if}
</main>
