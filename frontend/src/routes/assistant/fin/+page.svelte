<script lang="ts">
  // Wizard screen 5: what ends the first run (EF-UX-001, EF-USR-023, EF-INT-015, EF-SRC-024).
  // The jurisdiction is confirmed; the keyring leaves the instance under a passphrase and the kit
  // that says how to get back in is printed; a second admin is offered; the first book is
  // launched. The way out ("Terminer") is the server's to open: it appears when the server says
  // the keyring was exported and the country confirmed, never because a box was ticked here.
  // Every write is an existing route of the admin on a fresh session (a stale password is asked
  // in place); a secret typed here (the passphrase, the second admin's password) is sent once,
  // then cleared, and never shown again.
  import { onMount } from 'svelte';
  import {
    acquire,
    createProfile,
    patchConfig,
    wizardKeyringExport,
    wizardState
  } from '#lib/api/index.ts';
  import type { WizardStateOut } from '#lib/api/types.gen.ts';
  import ProblemAlert from '#lib/ui/molecules/ProblemAlert.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import WizardPage from '#lib/components/WizardPage.svelte';
  import WizardSignedOut from '#lib/components/WizardSignedOut.svelte';
  import { t } from '#lib/i18n/index.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import { csrfToken } from '#lib/passkeys.ts';
  import type { ProblemView } from '#lib/problems.ts';
  import { attempt, type Outcome } from '#lib/wizard.ts';

  const MIN_PASSPHRASE = 12; // the server's rule, told early so no request leaves for nothing
  const RECOVERY_KIT = '/api/v1/admin/recovery-kit';

  let facts = $state<WizardStateOut | null>(null);
  let busy = $state(false);
  let problem = $state<ProblemView | null>(null);
  let reauthMethods = $state<string[] | null>(null);
  let signedOut = $state(false);
  let retry: (() => Promise<boolean>) | null = null;

  let country = $state('');
  let phrase = $state('');
  let exportedNow = $state(false);
  let adminName = $state('');
  let adminPassword = $state('');
  let adminDone = $state(false);
  let bookId = $state('');
  let bookDone = $state(false);

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

  async function load(): Promise<void> {
    const { data } = await wizardState({ throwOnError: true });
    facts = data;
    country = data.jurisdiction.country;
  }
  onMount(() => guarded(load));

  const confirmJurisdiction = () =>
    guarded(async () => {
      await patchConfig({
        headers: headers(),
        body: { values: { 'instance.jurisdiction': country.trim().toUpperCase() } },
        throwOnError: true
      });
      await load();
    });

  /** Hand the age file to the browser, once; it is not kept on the page. */
  function offer(file: string): void {
    const url = URL.createObjectURL(new Blob([file], { type: 'text/plain' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'keyring.age';
    link.rel = 'noopener';
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  const exportKeyring = () =>
    guarded(async () => {
      const { data } = await wizardKeyringExport({
        headers: headers(),
        body: { passphrase: phrase },
        throwOnError: true
      });
      offer(String(data));
      phrase = '';
      exportedNow = true;
      await load(); // what the server recorded decides whether the admin may finish
    });

  const addSecondAdmin = () =>
    guarded(async () => {
      await createProfile({
        headers: headers(),
        body: { username: adminName.trim(), password: adminPassword, role: 'admin' },
        throwOnError: true
      });
      adminPassword = '';
      adminDone = true;
    });

  const launchFirstBook = () =>
    guarded(async () => {
      await acquire({
        headers: headers(),
        body: { source_key: 'gutenberg', remote_id: bookId.trim() },
        throwOnError: true
      });
      bookDone = true;
    });

  async function afterReauth(): Promise<void> {
    const again = retry;
    reauthMethods = null;
    if (again) await again();
  }

  const canFinish = $derived(Boolean(facts?.keyring_exported && facts.jurisdiction.confirmed));
</script>

<svelte:head>
  <title>{t('admin.wizard.fin.title')}</title>
</svelte:head>

<WizardPage current="fin" title={t('admin.wizard.fin.title')}>
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

    {#if !facts && !problem}
      <div aria-busy="true">
        <p class="visually-hidden" role="status">{t('admin.wizard.loading')}</p>
        <Skeleton /><Skeleton /><Skeleton short />
      </div>
    {/if}

    {#if facts}
      <section class="panel step" aria-labelledby="fin-jurisdiction">
        <h3 id="fin-jurisdiction">
          {t('admin.wizard.fin.jurisdiction')}
          {#if facts.jurisdiction.confirmed}<Badge tone="success">{t('admin.wizard.fin.jurisdiction_confirmed')}</Badge>{/if}
        </h3>
        {#if !facts.jurisdiction.confirmed}
          <p>{t('admin.wizard.fin.country', { country: facts.jurisdiction.country })}</p>
          <Field label={t('admin.wizard.fin.country_code')}>
            {#snippet children(control)}
              <Input {...control} bind:value={country} maxlength={2} autocomplete="off" width="short" />
            {/snippet}
          </Field>
          <div class="btn-row">
            <Button disabled={country.trim().length !== 2} loading={busy} onclick={confirmJurisdiction}>
              {t('admin.wizard.fin.confirm_jurisdiction')}
            </Button>
          </div>
        {/if}
      </section>

      <section class="panel step" aria-labelledby="fin-keyring">
        <h3 id="fin-keyring">{t('admin.wizard.fin.keyring')}</h3>
        <p>{t('admin.wizard.fin.keyring_help')}</p>
        <Field label={t('admin.wizard.fin.keyring_phrase')}>
          {#snippet children(control)}
            <Input {...control} type="password" bind:value={phrase} autocomplete="new-password" spellcheck={false} />
          {/snippet}
        </Field>
        <div class="btn-row">
          <Button icon="download" disabled={phrase.length < MIN_PASSPHRASE} loading={busy} onclick={exportKeyring}>
            {t('admin.wizard.fin.export_keyring')}
          </Button>
        </div>
        {#if exportedNow}<Alert tone="success" title={t('admin.wizard.fin.keyring_exported')} live="polite" />{/if}
        <p class="hint">{t('admin.wizard.fin.recovery_kit_help')}</p>
        <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- a page of the server, in its own tab -->
        <a class="standalone" href={RECOVERY_KIT} target="_blank" rel="noopener">{t('admin.wizard.fin.recovery_kit')}</a>
      </section>

      <section class="panel step" aria-labelledby="fin-admin">
        <h3 id="fin-admin">{t('admin.wizard.fin.second_admin')}</h3>
        <p>{t('admin.wizard.fin.second_admin_help')}</p>
        <Field label={t('admin.wizard.fin.admin_name')}>
          {#snippet children(control)}
            <Input {...control} bind:value={adminName} autocomplete="off" disabled={adminDone} />
          {/snippet}
        </Field>
        <Field label={t('admin.wizard.fin.admin_password')}>
          {#snippet children(control)}
            <Input {...control} type="password" bind:value={adminPassword} autocomplete="new-password" disabled={adminDone} />
          {/snippet}
        </Field>
        <div class="btn-row">
          <Button
            icon="user"
            disabled={adminDone || adminName.trim() === '' || adminPassword === ''}
            loading={busy}
            onclick={addSecondAdmin}
          >
            {t('admin.wizard.fin.add_admin')}
          </Button>
        </div>
        {#if adminDone}<Alert tone="success" title={t('admin.wizard.fin.admin_added')} live="polite" />{/if}
      </section>

      <section class="panel step" aria-labelledby="fin-book">
        <h3 id="fin-book">{t('admin.wizard.fin.first_book')}</h3>
        <Field label={t('admin.wizard.fin.book_id')}>
          {#snippet children(control)}
            <Input {...control} bind:value={bookId} inputmode="numeric" autocomplete="off" disabled={bookDone} width="short" />
          {/snippet}
        </Field>
        <div class="btn-row">
          <Button icon="read" disabled={bookDone || bookId.trim() === ''} loading={busy} onclick={launchFirstBook}>
            {t('admin.wizard.fin.launch_book')}
          </Button>
        </div>
        {#if bookDone}<Alert tone="success" title={t('admin.wizard.fin.book_launched')} live="polite" />{/if}
      </section>

      <div class="finish">
        {#if canFinish}
          <Button href="/" variant="primary" size="lg" iconEnd="right">{t('admin.wizard.fin.finish')}</Button>
        {:else}
          <Alert tone="info" title={t('admin.wizard.fin.finish_blocked')} />
        {/if}
      </div>
    {/if}
  {/if}
</WizardPage>
