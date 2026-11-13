<script lang="ts">
  import { onMount } from 'svelte';
  import { precreate } from '#lib/api/index.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import SentenceShelf from '#lib/components/SentenceShelf.svelte';
  import WizardSteps from '#lib/components/WizardSteps.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, needsSession, problemOf } from '#lib/triage.ts';
  import { nextStep } from '#lib/wizard.ts';

  // The shelves Shelfhold creates on a fresh instance (EF-UX-007): one sentence each.
  const PRECREATED = [
    { name: 'Manga', phrase: 'Les mangas vont dans Kavita › Manga' },
    { name: 'Comics et BD', phrase: 'Les comics et les BD vont dans Kavita › Comics et BD' },
    { name: 'Livres', phrase: 'Les livres vont dans Kavita › Livres' },
    { name: 'Famille', phrase: 'Ce que les enfants lisent va dans Kavita › Famille (13 ans au plus)' }
  ];

  let busy = $state(false);
  let done = $state(false);
  let problem = $state<ProblemView | null>(null);
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;
  let signedOut = $state(false);
  const next = nextStep('etageres');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  async function run(): Promise<void> {
    busy = true;
    problem = null;
    try {
      await precreate({ headers: headers(), body: { adopted: [] }, throwOnError: true });
      done = true;
    } catch (error) {
      const p = problemOf(error);
      if (p.reauth_methods) {
        retry = run;
        reauthMethods = p.reauth_methods ?? ['password'];
      } else if (needsSession(error)) {
        signedOut = true;
      } else {
        problem = problemView(error);
      }
    } finally {
      busy = false;
    }
  }

  async function afterReauth(): Promise<void> {
    reauthMethods = null;
    await run();
  }
</script>

<svelte:head>
  <title>{t('admin.wizard.title')}</title>
</svelte:head>

<main>
  <h1>{t('admin.wizard.title')}</h1>
  <WizardSteps current="etageres" />
  <h2>{t('admin.wizard.etageres.title')}</h2>
  <p>{t('admin.wizard.etageres.explain')}</p>

  <details>
    <summary>{t('admin.wizard.etageres.advanced')}</summary>
    <p>{t('admin.wizard.etageres.advanced_help')}</p>
  </details>

  <ul>
    {#each PRECREATED as shelf (shelf.name)}
      <li><SentenceShelf name={shelf.name} phrase={shelf.phrase} /></li>
    {/each}
  </ul>

  <section>
    <h3>{t('admin.wizard.sources.title')}</h3>
    <ul>
      <li>Gutenberg <span>{t('admin.wizard.sources.family')}</span></li>
      <li>Wikisource <span>{t('admin.wizard.sources.family')}</span></li>
    </ul>
  </section>

  {#if !done}
    <button type="button" disabled={busy} onclick={run}>
      {t('admin.wizard.etageres.create')}
    </button>
  {:else}
    <p role="status">{t('admin.wizard.etageres.created')}</p>
  {/if}

  {#if problem}<ProblemCard {problem} />{/if}
  {#if signedOut}<p role="alert">{t('admin.wizard.signed_out')}</p>{/if}
  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} onsuccess={afterReauth} oncancel={() => { reauthMethods = null; }} />
  {/if}

  {#if done && next}
    <a href={next.href}>{t('admin.wizard.next')}</a>
  {/if}
</main>