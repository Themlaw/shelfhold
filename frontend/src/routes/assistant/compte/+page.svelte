<script lang="ts">
  // Wizard screen 1: the account. The administrator account is made at installation; this screen
  // says who is signed in and sends whoever is not to the sign-in page.
  import { onMount } from 'svelte';
  import { me } from '#lib/api/index.ts';
  import WizardSteps from '#lib/components/WizardSteps.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { nextStep } from '#lib/wizard.ts';

  let name = $state<string | null>(null);
  let isAdmin = $state(false);
  let signedOut = $state(false);
  let loaded = $state(false);
  const next = nextStep('compte');

  onMount(async () => {
    try {
      const { data } = await me({ throwOnError: true });
      name = data.username;
      isAdmin = data.role === 'admin';
    } catch {
      signedOut = true;
    } finally {
      loaded = true;
    }
  });
</script>

<svelte:head>
  <title>{t('admin.wizard.title')}</title>
</svelte:head>

<main>
  <h1>{t('admin.wizard.title')}</h1>
  <WizardSteps current="compte" />
  <h2>{t('admin.wizard.compte.title')}</h2>

  {#if loaded && signedOut}
    <p role="alert">{t('admin.wizard.signed_out')}</p>
    <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
    <p><a href="/login">{t('admin.wizard.login')}</a></p>
  {:else if loaded && name !== null}
    <p role="status">{t('admin.wizard.compte.signed_in', { name })}</p>
    {#if isAdmin && next}
      <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
      <p><a href={next.href}>{t('admin.wizard.next')}</a></p>
    {:else if !isAdmin}
      <p role="alert">{t('admin.wizard.compte.not_admin')}</p>
    {/if}
  {/if}
</main>
