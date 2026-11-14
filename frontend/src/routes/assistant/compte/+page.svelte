<script lang="ts">
  // Wizard screen 1: the account. On a new instance the administrator account is made here, with
  // the secret the installer put in the compose (EF-UX-001): one submission, and it signs in.
  // Otherwise the screen says who is signed in and sends whoever is not to the sign-in page.
  import { onDestroy, onMount } from 'svelte';
  import { firstRunStatus, makeFirstAdmin, me } from '#lib/api/index.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import WizardSteps from '#lib/components/WizardSteps.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { advanceFrom, nextStep } from '#lib/wizard.ts';

  let name = $state<string | null>(null);
  let isAdmin = $state(false);
  let signedOut = $state(false);
  let loaded = $state(false);
  let fresh = $state<{ tokenConfigured: boolean } | null>(null); // a new instance: no admin yet
  let username = $state('');
  let password = $state('');
  let setupToken = $state('');
  let busy = $state(false);
  let problem = $state<ProblemView | null>(null);
  const next = nextStep('compte');
  let cancelAdvance = () => {};
  onDestroy(() => cancelAdvance());

  onMount(async () => {
    try {
      const { data } = await me({ throwOnError: true });
      name = data.username;
      isAdmin = data.role === 'admin';
      // nothing to ask here: the admin goes on by itself (only an admin: the others stay and read why)
      if (isAdmin) cancelAdvance = advanceFrom('compte');
    } catch {
      signedOut = true;
      try {
        const { data } = await firstRunStatus({ throwOnError: true });
        if (data.needs_admin) fresh = { tokenConfigured: data.token_configured };
      } catch {
        // the status cannot be read: the sign-in page stays the way out
      }
    } finally {
      loaded = true;
    }
  });

  async function createAccount(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    problem = null;
    try {
      await makeFirstAdmin({
        body: { username: username.trim(), password, setup_token: setupToken },
        throwOnError: true
      });
      name = username.trim();
      isAdmin = true;
      signedOut = false;
      fresh = null;
      password = '';
      setupToken = '';
      cancelAdvance = advanceFrom('compte');
    } catch (error) {
      problem = problemView(error);
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head>
  <title>{t('admin.wizard.title')}</title>
</svelte:head>

<main>
  <h1>{t('admin.wizard.title')}</h1>
  <WizardSteps current="compte" />
  <h2>{t('admin.wizard.compte.title')}</h2>

  {#if loaded && signedOut && fresh}
    {#if fresh.tokenConfigured}
      <form onsubmit={createAccount} aria-label={t('admin.wizard.compte.create')}>
        <p>{t('admin.wizard.compte.intro')}</p>
        <label>
          {t('admin.wizard.compte.username')}
          <input bind:value={username} required maxlength="128" autocomplete="username" />
        </label>
        <label>
          {t('admin.wizard.compte.password')}
          <input
            type="password"
            bind:value={password}
            required
            minlength="12"
            autocomplete="new-password"
          />
        </label>
        <label>
          {t('admin.wizard.compte.token')}
          <input type="password" bind:value={setupToken} required autocomplete="off" />
        </label>
        <p class="hint">{t('admin.wizard.compte.token_help')}</p>
        {#if problem}
          <ProblemCard {problem} />
        {/if}
        <button type="submit" disabled={busy}>{t('admin.wizard.compte.submit')}</button>
      </form>
    {:else}
      <p role="alert">{t('admin.wizard.compte.no_token')}</p>
    {/if}
  {:else if loaded && signedOut}
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
