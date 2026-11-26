<script lang="ts">
  // Wizard screen 2: the Kavita connection (EF-UX-001, D12). The credentials are exchanged, once,
  // for a named key that expires; the server keeps only that key. Here they live in memory for
  // the time of the request and are wiped as soon as it is over (kept only while the password
  // is asked again, because the same request is then sent again).
  import { onDestroy } from 'svelte';
  import { addTarget, wizardState } from '#lib/api/index.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import WizardNext from '#lib/components/WizardNext.svelte';
  import WizardPage from '#lib/components/WizardPage.svelte';
  import WizardSignedOut from '#lib/components/WizardSignedOut.svelte';
  import { formatDate, t } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Checkbox from '#lib/ui/atoms/Checkbox.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, needsSession, problemOf } from '#lib/triage.ts';
  import { advanceFrom, nextStep, parsePathMap } from '#lib/wizard.ts';

  let url = $state('');
  let username = $state('');
  let password = $state('');
  let accepted = $state(false);
  let pathMap = $state('');
  let mapInvalid = $state(false);
  let cancelAdvance = () => {};
  onDestroy(() => cancelAdvance());
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
    // the map is read first: a wrong one is refused before the password leaves this page
    const map = parsePathMap(pathMap);
    mapInvalid = map === null;
    if (map === null) return;
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
          accept_admin_risk: accepted,
          ...(map ? { path_map: map } : {})
        },
        throwOnError: true
      });
      wipe();
      done = true;
      cancelAdvance = advanceFrom('kavita');
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

<WizardPage current="kavita" title={t('admin.wizard.kavita.title')}>
  <div role="note">
    <Alert tone="warning" title={t('admin.wizard.kavita.warning_title')}>
      <p>{t('admin.wizard.kavita.admin_warning')}</p>
    </Alert>
  </div>

  {#if signedOut}
    <WizardSignedOut />
  {:else if done}
    <Alert tone="success" title={t('admin.wizard.kavita.done')} live="polite">
      {#if expires}
        <p>{t('admin.wizard.kavita.expires', { date: formatDate(expires) })}</p>
      {/if}
    </Alert>
    {#if next}
      <WizardNext href={next.href} />
    {/if}
  {:else}
    {#if reauthMethods}
      <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
    {/if}
    {#if problem}
      <ProblemCard {problem} />
    {/if}
    <form class="connect" onsubmit={submit} autocomplete="off">
      <Field label={t('admin.wizard.kavita.url')} required>
        {#snippet children(control)}
          <Input {...control} type="url" bind:value={url} placeholder={t('admin.wizard.kavita.url_placeholder')} mono />
        {/snippet}
      </Field>
      <div class="pair">
        <Field label={t('admin.wizard.kavita.username')} required>
          {#snippet children(control)}
            <Input {...control} type="text" bind:value={username} autocomplete="off" />
          {/snippet}
        </Field>
        <Field label={t('admin.wizard.kavita.password')} required>
          {#snippet children(control)}
            <Input {...control} type="password" bind:value={password} autocomplete="new-password" />
          {/snippet}
        </Field>
      </div>
      <Checkbox bind:checked={accepted} label={t('admin.wizard.kavita.accept')} />
      <fieldset class="map">
        <legend>{t('admin.wizard.kavita.map_title')}</legend>
        <p class="hint">{t('admin.wizard.kavita.map_help')}</p>
        <Field label={t('admin.wizard.kavita.map')}>
          {#snippet children(control)}
            <Input
              {...control}
              type="text"
              bind:value={pathMap}
              autocomplete="off"
              placeholder={t('admin.wizard.kavita.map_placeholder')}
              mono
            />
          {/snippet}
        </Field>
        {#if mapInvalid}
          <Alert tone="danger" title={t('admin.wizard.kavita.map_invalid')} live="assertive" />
        {/if}
      </fieldset>
      <div class="btn-row">
        <Button type="submit" variant="primary" size="lg" icon="plug" disabled={!accepted || reauthMethods !== null} loading={busy}>
          {t('admin.wizard.kavita.connect')}
        </Button>
      </div>
    </form>
  {/if}
</WizardPage>

<style>
  .connect {
    display: grid;
    gap: var(--space-4);
  }
  .pair {
    display: grid;
    gap: var(--space-4);
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 14rem), 1fr));
  }
  .map {
    margin: 0;
    padding: var(--space-4);
    border: var(--rule-hair) solid var(--color-rule);
    border-radius: var(--radius-sm);
    display: grid;
    gap: var(--space-2);
  }
  .map .hint {
    margin: 0;
  }
</style>
