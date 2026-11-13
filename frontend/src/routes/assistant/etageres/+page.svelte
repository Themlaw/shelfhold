<script lang="ts">
  // Wizard screen 4: the shelves, in sentences, and the sources a child may search (EF-UX-007,
  // EF-UX-001, D13). The sentences are the server's own, shown before and after the shelves
  // exist; one button makes the shelves (L33's route) and records the family sources, so the
  // admin who accepts what is proposed does it in one action. Nothing here decides who may.
  import { onMount } from 'svelte';
  import { precreate, wizardMarkFamily, wizardState } from '#lib/api/index.ts';
  import type { WizardShelfOut, WizardSourceOut } from '#lib/api/types.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import SentenceShelf from '#lib/components/SentenceShelf.svelte';
  import WizardSteps from '#lib/components/WizardSteps.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import type { ProblemView } from '#lib/problems.ts';
  import { attempt, FAMILY_BY_DEFAULT, nextStep, SOURCE_NAMES, type Outcome } from '#lib/wizard.ts';

  let shelves = $state<WizardShelfOut[]>([]);
  let sources = $state<WizardSourceOut[]>([]);
  let family = $state<string[]>([]);
  let loaded = $state(false);
  let done = $state(false);
  let busy = $state(false);
  let problem = $state<ProblemView | null>(null);
  let reauthMethods = $state<string[] | null>(null);
  let signedOut = $state(false);
  let retry: (() => Promise<boolean>) | null = null;
  const next = nextStep('etageres');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  const outcome: Outcome = {
    reauth: (methods) => (reauthMethods = methods),
    signedOut: () => (signedOut = true),
    problem: (view) => (problem = view)
  };

  async function guarded(action: () => Promise<void>): Promise<boolean> {
    busy = true;
    problem = null;
    retry = () => guarded(action);
    try {
      return await attempt(action, outcome);
    } finally {
      busy = false;
    }
  }

  onMount(() =>
    guarded(async () => {
      const { data } = await wizardState({ throwOnError: true });
      shelves = data.shelves;
      sources = data.sources;
      // an instance that already marked some sources keeps its own choice; a fresh one proposes
      const marked = data.sources.filter((s) => s.family).map((s) => s.key);
      family = marked.length > 0 ? marked : data.sources.map((s) => s.key).filter((k) => FAMILY_BY_DEFAULT.includes(k));
      loaded = true;
    })
  );

  const create = () =>
    guarded(async () => {
      await precreate({ headers: headers(), body: { adopted: [] }, throwOnError: true });
      await wizardMarkFamily({ headers: headers(), body: { source_keys: family }, throwOnError: true });
      done = true;
    });

  async function afterReauth(): Promise<void> {
    const again = retry;
    reauthMethods = null;
    if (again) await again();
  }
</script>

<svelte:head>
  <title>{t('admin.wizard.etageres.title')}</title>
</svelte:head>

<main>
  <h1>{t('admin.wizard.title')}</h1>
  <WizardSteps current="etageres" />
  <h2>{t('admin.wizard.etageres.title')}</h2>

  {#if signedOut}
    <p role="alert">{t('admin.wizard.signed_out')}</p>
    <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
    <p><a href="/login">{t('admin.wizard.login')}</a></p>
  {:else}
    {#if reauthMethods}
      <ReauthPrompt
        methods={reauthMethods}
        ondone={afterReauth}
        oncancel={() => (reauthMethods = null)}
      />
    {/if}
    {#if problem}<ProblemCard {problem} />{/if}

    {#if loaded}
      <p>{t('admin.wizard.etageres.explain')}</p>
      <ul>
        {#each shelves as shelf (shelf.name)}
          <li><SentenceShelf {shelf} /></li>
        {/each}
      </ul>

      <details>
        <summary>{t('admin.wizard.etageres.advanced')}</summary>
        <p>{t('admin.wizard.etageres.advanced_help')}</p>
      </details>

      <fieldset>
        <legend>{t('admin.wizard.sources.title')}</legend>
        <p>{t('admin.wizard.sources.explain')}</p>
        {#each sources as source (source.key)}
          <label>
            <input type="checkbox" value={source.key} bind:group={family} disabled={done} />
            {t(SOURCE_NAMES[source.key] ?? 'admin.wizard.sources.name.other')}
          </label>
        {/each}
      </fieldset>

      {#if !done}
        <button type="button" disabled={busy} onclick={create}>
          {t('admin.wizard.etageres.create')}
        </button>
      {:else}
        <p role="status">{t('admin.wizard.etageres.created')}</p>
        {#if next}
          <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
          <a href={next.href}>{t('admin.wizard.next')}</a>
        {/if}
      {/if}
    {/if}
  {/if}
</main>
