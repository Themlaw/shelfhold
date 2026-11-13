<script lang="ts">
  // Wizard screen 2: the Kavita connection (EF-UX-001, D12). The credentials are exchanged, once,
  // for a named key that expires; the server keeps only that key. Here they live in memory for
  // the time of the request and are wiped as soon as it is over (kept only while the password
  // is asked again, because the same request is then sent again).
  import { addTarget, wizardState } from '#lib/api/index.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import WizardSteps from '#lib/components/WizardSteps.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, needsSession, problemOf } from '#lib/triage.ts';
  import { nextStep } from '#lib/wizard.ts';

  let url = $state('');
  let username = $state('');
  let password = $state('');
  let accepted = $state(false);
  let busy = $state(false);
  let done = $state(false);
  let expires = $state<string | null>(null);
  let signedOut = $state(false);
  let problem = $state<ProblemView | null>(null);
  let reauthMethods = $state<string[] | null>(null);
  const next = nextStep('kavita');

  const wipe = () => {
    password = '';
    username = '';
  };

  async function connect(): Promise<void> {
    busy = true;
    problem = null;
    try {
      await addTarget({
        headers: { 'X-CSRF-Token': csrfToken() },
        body: {
          kind: 'kavita',
          name: 'kavita',
          url,
          username,
          password,
          accept_admin_risk: accepted
        },
        throwOnError: true
      });
      wipe();
      done = true;
      const { data } = await wizardState({ throwOnError: true });
      expires = data.targets[0]?.key_expires_at ?? null;
    } catch (error) {
      if (needsReauth(error)) {
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else if (needsSession(error)) {
        wipe();
        signedOut = true;
      } else {
        wipe();
        problem = problemView(error);
      }
    } finally {
      busy = false;
    }
  }

  const submit = (event: SubmitEvent) => {
    event.preventDefault();
    return connect();
  };

  async function afterReauth(): Promise<void> {
    reauthMethods = null;
    await connect();
  }

  function cancelReauth(): void {
    reauthMethods = null;
    wipe();
  }
</script>

<svelte:head>
  <title>{t('admin.wizard.kavita.title')}</title>
</svelte:head>

<main>
  <h1>{t('admin.wizard.title')}</h1>
  <WizardSteps current="kavita" />
  <h2>{t('admin.wizard.kavita.title')}</h2>
  <p role="note" class="warning">{t('admin.wizard.kavita.admin_warning')}</p>

  {#if signedOut}
    <p role="alert">{t('admin.wizard.signed_out')}</p>
    <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
    <p><a href="/login">{t('admin.wizard.login')}</a></p>
  {:else if done}
    <p role="status">{t('admin.wizard.kavita.done')}</p>
    {#if expires}
      <p>{t('admin.wizard.kavita.expires', { date: new Date(expires).toLocaleDateString('fr-FR') })}</p>
    {/if}
    {#if next}
      <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
      <p><a href={next.href}>{t('admin.wizard.next')}</a></p>
    {/if}
  {:else}
    {#if reauthMethods}
      <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
    {/if}
    {#if problem}
      <ProblemCard {problem} />
    {/if}
    <form onsubmit={submit} autocomplete="off">
      <label>
        {t('admin.wizard.kavita.url')}
        <input type="url" bind:value={url} required placeholder="http://kavita:5000" />
      </label>
      <label>
        {t('admin.wizard.kavita.username')}
        <input type="text" bind:value={username} required autocomplete="off" />
      </label>
      <label>
        {t('admin.wizard.kavita.password')}
        <input type="password" bind:value={password} required autocomplete="new-password" />
      </label>
      <label class="check">
        <input type="checkbox" bind:checked={accepted} />
        {t('admin.wizard.kavita.accept')}
      </label>
      <button type="submit" disabled={busy || !accepted || reauthMethods !== null}>
        {t('admin.wizard.kavita.connect')}
      </button>
    </form>
  {/if}
</main>
