<script lang="ts">
  import { onMount } from 'svelte';
  import {
    acquire,
    createProfile,
    me,
    patchConfig,
    readConfig,
    recoveryKit,
    wizardState
  } from '#lib/api/index.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import WizardSteps from '#lib/components/WizardSteps.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, needsSession, problemOf } from '#lib/triage.ts';
  import { nextStep } from '#lib/wizard.ts';

  let jurisdiction = $state('FR');
  let confirmed = $state(false);
  let keyringExported = $state(false);
  let busy = $state(false);
  let problem = $state<ProblemView | null>(null);
  let reauthMethods = $state<string[] | null>(null);
  let signedOut = $state(false);

  // second admin
  let adminName = $state('');
  let adminPassword = $state('');
  let adminDone = $state(false);
  // first book
  let bookId = $state('');
  let bookDone = $state(false);

  const next = nextStep('fin');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  onMount(async () => {
    try {
      const { data } = await readConfig({ throwOnError: true });
      const row = data.items.find((i: { key: string }) => i.key === 'instance.jurisdiction');
      if (row) jurisdiction = String(row.value ?? row.default ?? 'FR');
      const cfg = await wizardState({ throwOnError: true }).catch(() => null);
      if (cfg) confirmed = false;
    } catch {
      // ignore
    }
  });

  async function confirmJurisdiction(): Promise<void> {
    busy = true;
    problem = null;
    try {
      await patchConfig({
        headers: headers(),
        body: { values: { 'instance.jurisdiction': jurisdiction } },
        throwOnError: true
      });
      confirmed = true;
    } catch (error) {
      const p = problemOf(error);
      if (p.reauth_methods) reauthMethods = p.reauth_methods ?? ['password'];
      else if (needsSession(error)) signedOut = true;
      else problem = problemView(error);
    } finally {
      busy = false;
    }
  }

  async function openRecoveryKit(): Promise<void> {
    try {
      const { data } = await recoveryKit({ throwOnError: true });
      const w = window.open();
      if (w) {
        w.document.write(typeof data === 'string' ? data : String(data));
        w.document.close();
      }
    } catch (error) {
      problem = problemView(error);
    }
  }

  async function addSecondAdmin(): Promise<void> {
    busy = true;
    problem = null;
    try {
      await createProfile({
        headers: headers(),
        body: { username: adminName, password: adminPassword, role: 'admin' },
        throwOnError: true
      });
      adminDone = true;
    } catch (error) {
      const p = problemOf(error);
      if (p.reauth_methods) reauthMethods = p.reauth_methods ?? ['password'];
      else if (needsSession(error)) signedOut = true;
      else problem = problemView(error);
    } finally {
      busy = false;
    }
  }

  async function launchFirstBook(): Promise<void> {
    busy = true;
    problem = null;
    try {
      await acquire({
        headers: headers(),
        body: { source_key: 'gutenberg', remote_id: bookId || null },
        throwOnError: true
      });
      bookDone = true;
    } catch (error) {
      const p = problemOf(error);
      if (p.reauth_methods) reauthMethods = p.reauth_methods ?? ['password'];
      else if (needsSession(error)) signedOut = true;
      else problem = problemView(error);
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
  <WizardSteps current="fin" />
  <h2>{t('admin.wizard.fin.title')}</h2>

  <section aria-labelledby="jur">
    <h3 id="jur">{t('admin.wizard.fin.jurisdiction')}</h3>
    <input bind:value={jurisdiction} aria-label={t('admin.wizard.fin.jurisdiction')} />
    <button type="button" disabled={busy || confirmed} onclick={confirmJurisdiction}>
      {t('admin.wizard.fin.confirm_jurisdiction')}
    </button>
    {#if confirmed}<p role="status">{t('admin.wizard.fin.jurisdiction_confirmed')}</p>{/if}
  </section>

  <section aria-labelledby="keyring">
    <h3 id="keyring">{t('admin.wizard.fin.keyring')}</h3>
    <p>{t('admin.wizard.fin.keyring_help')}</p>
    <button type="button" onclick={openRecoveryKit}>{t('admin.wizard.fin.recovery_kit')}</button>
    <label>
      <input type="checkbox" bind:checked={keyringExported} />
      {t('admin.wizard.fin.keyring_done')}
    </label>
  </section>

  <section aria-labelledby="admin2">
    <h3 id="admin2">{t('admin.wizard.fin.second_admin')}</h3>
    <input bind:value={adminName} placeholder={t('admin.wizard.fin.admin_name')} aria-label={t('admin.wizard.fin.admin_name')} />
    <input type="password" bind:value={adminPassword} aria-label={t('admin.wizard.fin.admin_password')} />
    <button type="button" disabled={busy || adminDone} onclick={addSecondAdmin}>
      {t('admin.wizard.fin.add_admin')}
    </button>
    {#if adminDone}<p role="status">{t('admin.wizard.fin.admin_added')}</p>{/if}
  </section>

  <section aria-labelledby="book">
    <h3 id="book">{t('admin.wizard.fin.first_book')}</h3>
    <input bind:value={bookId} aria-label={t('admin.wizard.fin.book_id')} />
    <button type="button" disabled={busy || bookDone} onclick={launchFirstBook}>
      {t('admin.wizard.fin.launch_book')}
    </button>
    {#if bookDone}<p role="status">{t('admin.wizard.fin.book_launched')}</p>{/if}
  </section>

  {#if problem}<ProblemCard {problem} />{/if}
  {#if signedOut}<p role="alert">{t('admin.wizard.signed_out')}</p>{/if}
  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} onsuccess={() => { reauthMethods = null; }} oncancel={() => { reauthMethods = null; }} />
  {/if}

  {#if keyringExported}
    <a href="/">{t('admin.wizard.fin.finish')}</a>
  {/if}
</main>