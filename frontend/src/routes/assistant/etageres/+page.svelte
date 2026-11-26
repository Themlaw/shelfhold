<script lang="ts">
  // Wizard screen 4: the shelves, in sentences, and the sources a child may search (EF-UX-007,
  // EF-UX-001, D13). The sentences are the server's own, shown before and after the shelves
  // exist; one button makes the shelves (L33's route) and records the family sources, so the
  // admin who accepts what is proposed does it in one action. Nothing here decides who may.
  import { onDestroy, onMount } from 'svelte';
  import { precreate, wizardMarkFamily, wizardState } from '#lib/api/index.ts';
  import type { WizardShelfOut, WizardSourceOut } from '#lib/api/types.gen.ts';
  import ProblemAlert from '#lib/ui/molecules/ProblemAlert.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import SentenceShelf from '#lib/components/SentenceShelf.svelte';
  import WizardNext from '#lib/components/WizardNext.svelte';
  import WizardPage from '#lib/components/WizardPage.svelte';
  import WizardSignedOut from '#lib/components/WizardSignedOut.svelte';
  import { t } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Checkbox from '#lib/ui/atoms/Checkbox.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import { csrfToken } from '#lib/passkeys.ts';
  import type { ProblemView } from '#lib/problems.ts';
  import {
    advanceFrom,
    attempt,
    FAMILY_BY_DEFAULT,
    nextStep,
    SOURCE_NAMES,
    type Outcome
  } from '#lib/wizard.ts';

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
  let cancelAdvance = () => {};
  onDestroy(() => cancelAdvance());

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
      cancelAdvance = advanceFrom('etageres');
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

<WizardPage current="etageres" title={t('admin.wizard.etageres.title')}>
  {#if signedOut}
    <WizardSignedOut />
  {:else}
    {#if reauthMethods}
      <ReauthPrompt
        methods={reauthMethods}
        ondone={afterReauth}
        oncancel={() => (reauthMethods = null)}
      />
    {/if}
    {#if problem}<ProblemAlert {problem} />{/if}

    {#if !loaded}
      <div aria-busy="true">
        <p class="visually-hidden" role="status">{t('admin.wizard.loading')}</p>
        <Skeleton /><Skeleton /><Skeleton short />
      </div>
    {:else}
      <p class="intro">{t('admin.wizard.etageres.explain')}</p>
      <ul class="shelves" role="list">
        {#each shelves as shelf (shelf.name)}
          <li><SentenceShelf {shelf} /></li>
        {/each}
      </ul>

      <details class="advanced">
        <summary>{t('admin.wizard.etageres.advanced')}</summary>
        <p>{t('admin.wizard.etageres.advanced_help')}</p>
      </details>

      <fieldset class="sources">
        <legend>{t('admin.wizard.sources.title')}</legend>
        <p class="hint">{t('admin.wizard.sources.explain')}</p>
        {#each sources as source (source.key)}
          <Checkbox
            checked={family.includes(source.key)}
            disabled={done}
            value={source.key}
            label={t(SOURCE_NAMES[source.key] ?? 'admin.wizard.sources.name.other')}
            onchange={(event) => {
              const on = (event.currentTarget as HTMLInputElement).checked;
              family = on ? [...family, source.key] : family.filter((key) => key !== source.key);
            }}
          />
        {/each}
      </fieldset>

      {#if !done}
        <div class="btn-row">
          <Button variant="primary" size="lg" icon="books" loading={busy} onclick={create}>
            {t('admin.wizard.etageres.create')}
          </Button>
        </div>
      {:else}
        <Alert tone="success" title={t('admin.wizard.etageres.created')} live="polite" />
        {#if next}
          <WizardNext href={next.href} />
        {/if}
      {/if}
    {/if}
  {/if}
</WizardPage>
