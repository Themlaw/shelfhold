<script lang="ts">
  // Wizard screen 1: the account. On a new instance the administrator account is made here, with
  // the secret the installer put in the compose (EF-UX-001): one submission, and it signs in.
  // Otherwise the screen says who is signed in and sends whoever is not to the sign-in page.
  import { onDestroy, onMount } from 'svelte';
  import { firstRunStatus, makeFirstAdmin, me } from '#lib/api/index.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import WizardNext from '#lib/components/WizardNext.svelte';
  import WizardPage from '#lib/components/WizardPage.svelte';
  import WizardSignedOut from '#lib/components/WizardSignedOut.svelte';
  import { t } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
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

<WizardPage current="compte" title={t('admin.wizard.compte.title')}>
  {#if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('admin.wizard.loading')}</p>
      <Skeleton /><Skeleton /><Skeleton short />
    </div>
  {:else if signedOut && fresh}
    {#if fresh.tokenConfigured}
      <form class="create" onsubmit={createAccount} aria-label={t('admin.wizard.compte.create')}>
        <p class="intro">{t('admin.wizard.compte.intro')}</p>
        <Field label={t('admin.wizard.compte.username')} required>
          {#snippet children(control)}
            <Input {...control} bind:value={username} maxlength={128} autocomplete="username" />
          {/snippet}
        </Field>
        <Field label={t('admin.wizard.compte.password')} hint={t('admin.wizard.compte.password_help')} required>
          {#snippet children(control)}
            <Input {...control} type="password" bind:value={password} minlength={12} autocomplete="new-password" />
          {/snippet}
        </Field>
        <Field label={t('admin.wizard.compte.token')} hint={t('admin.wizard.compte.token_help')} required>
          {#snippet children(control)}
            <Input {...control} type="password" bind:value={setupToken} autocomplete="off" />
          {/snippet}
        </Field>
        {#if problem}
          <ProblemCard {problem} />
        {/if}
        <div class="btn-row">
          <Button type="submit" variant="primary" size="lg" loading={busy}>{t('admin.wizard.compte.submit')}</Button>
        </div>
      </form>
    {:else}
      <div role="alert">
        <Alert tone="danger" title={t('admin.wizard.compte.no_token_title')}>
          <p>{t('admin.wizard.compte.no_token')}</p>
        </Alert>
      </div>
    {/if}
  {:else if signedOut}
    <WizardSignedOut />
  {:else if name !== null}
    <Alert tone="success" title={t('admin.wizard.compte.signed_in', { name })} live="polite" />
    {#if isAdmin && next}
      <WizardNext href={next.href} />
    {:else if !isAdmin}
      <div role="alert">
        <Alert tone="warning" title={t('admin.wizard.compte.not_admin_title')}>
          <p>{t('admin.wizard.compte.not_admin')}</p>
        </Alert>
      </div>
    {/if}
  {/if}
</WizardPage>

<style>
  .create {
    display: grid;
    gap: var(--space-4);
  }
  .intro {
    margin: 0;
    max-width: var(--measure);
  }
</style>
