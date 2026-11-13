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
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import WizardSteps from '#lib/components/WizardSteps.svelte';
  import { t } from '#lib/i18n/index.ts';
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

<main>
  <h1>{t('admin.wizard.title')}</h1>
  <WizardSteps current="fin" />
  <h2>{t('admin.wizard.fin.title')}</h2>

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

    {#if facts}
      <section aria-labelledby="fin-jurisdiction">
        <h3 id="fin-jurisdiction">{t('admin.wizard.fin.jurisdiction')}</h3>
        {#if facts.jurisdiction.confirmed}
          <p role="status">{t('admin.wizard.fin.jurisdiction_confirmed')}</p>
        {:else}
          <p>{t('admin.wizard.fin.country', { country: facts.jurisdiction.country })}</p>
          <label>
            {t('admin.wizard.fin.country_code')}
            <input bind:value={country} maxlength="2" autocomplete="off" />
          </label>
          <button type="button" disabled={busy || country.trim().length !== 2} onclick={confirmJurisdiction}>
            {t('admin.wizard.fin.confirm_jurisdiction')}
          </button>
        {/if}
      </section>

      <section aria-labelledby="fin-keyring">
        <h3 id="fin-keyring">{t('admin.wizard.fin.keyring')}</h3>
        <p>{t('admin.wizard.fin.keyring_help')}</p>
        <label>
          {t('admin.wizard.fin.keyring_phrase')}
          <input
            type="password"
            bind:value={phrase}
            autocomplete="new-password"
            spellcheck="false"
          />
        </label>
        <button
          type="button"
          disabled={busy || phrase.length < MIN_PASSPHRASE}
          onclick={exportKeyring}
        >
          {t('admin.wizard.fin.export_keyring')}
        </button>
        {#if exportedNow}<p role="status">{t('admin.wizard.fin.keyring_exported')}</p>{/if}
        <p>{t('admin.wizard.fin.recovery_kit_help')}</p>
        <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- a page of the server, in its own tab -->
        <a class="standalone" href={RECOVERY_KIT} target="_blank" rel="noopener">{t('admin.wizard.fin.recovery_kit')}</a>
      </section>

      <section aria-labelledby="fin-admin">
        <h3 id="fin-admin">{t('admin.wizard.fin.second_admin')}</h3>
        <p>{t('admin.wizard.fin.second_admin_help')}</p>
        <label>
          {t('admin.wizard.fin.admin_name')}
          <input bind:value={adminName} autocomplete="off" disabled={adminDone} />
        </label>
        <label>
          {t('admin.wizard.fin.admin_password')}
          <input
            type="password"
            bind:value={adminPassword}
            autocomplete="new-password"
            disabled={adminDone}
          />
        </label>
        <button
          type="button"
          disabled={busy || adminDone || adminName.trim() === '' || adminPassword === ''}
          onclick={addSecondAdmin}
        >
          {t('admin.wizard.fin.add_admin')}
        </button>
        {#if adminDone}<p role="status">{t('admin.wizard.fin.admin_added')}</p>{/if}
      </section>

      <section aria-labelledby="fin-book">
        <h3 id="fin-book">{t('admin.wizard.fin.first_book')}</h3>
        <label>
          {t('admin.wizard.fin.book_id')}
          <input bind:value={bookId} inputmode="numeric" autocomplete="off" disabled={bookDone} />
        </label>
        <button
          type="button"
          disabled={busy || bookDone || bookId.trim() === ''}
          onclick={launchFirstBook}
        >
          {t('admin.wizard.fin.launch_book')}
        </button>
        {#if bookDone}<p role="status">{t('admin.wizard.fin.book_launched')}</p>{/if}
      </section>

      {#if canFinish}
        <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
        <a href="/">{t('admin.wizard.fin.finish')}</a>
      {:else}
        <p>{t('admin.wizard.fin.finish_blocked')}</p>
      {/if}
    {/if}
  {/if}
</main>
