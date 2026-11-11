<script lang="ts">
  import { onMount } from 'svelte';
  import {
    passkeyRegister,
    passkeyRegistrationOptions,
    passkeyRevoke,
    passkeysList
  } from '#lib/api/index.ts';
  import type { PasskeyItem } from '#lib/api/types.gen.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { createPasskey, csrfToken, passkeysSupported } from '#lib/passkeys.ts';
  import { needsReauth, problemOf } from '#lib/triage.ts';

  let items = $state<PasskeyItem[]>([]);
  let available = $state(false);
  let reason = $state<string | null>(null);
  let name = $state('');
  let message = $state('');
  let failed = $state(false);
  let busy = $state(false);
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  async function load(): Promise<void> {
    try {
      const { data } = await passkeysList({ throwOnError: true });
      items = data.items;
      available = data.available && passkeysSupported();
      reason = data.available ? t('passkeys.unsupported') : (data.reason ?? null);
    } catch {
      failed = true;
      message = t('passkeys.error');
    }
  }

  onMount(load);

  /** A stale password asks for it in place (EF-USR-022), then runs the same action again. */
  function askReauth(error: unknown, again: () => Promise<void>): boolean {
    if (!needsReauth(error)) return false;
    retry = again;
    reauthMethods = problemOf(error).reauth_methods ?? ['password'];
    failed = false;
    message = '';
    return true;
  }

  async function afterReauth(): Promise<void> {
    const again = retry;
    retry = null;
    reauthMethods = null;
    if (again) await again();
  }

  function cancelReauth(): void {
    retry = null;
    reauthMethods = null;
  }

  function register(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    return enrol();
  }

  async function enrol(): Promise<void> {
    busy = true;
    failed = false;
    message = '';
    try {
      const { data: started } = await passkeyRegistrationOptions({
        headers: headers(),
        throwOnError: true
      });
      const credential = await createPasskey(started.options);
      await passkeyRegister({
        headers: headers(),
        body: { flow: started.flow, name: name.trim(), credential },
        throwOnError: true
      });
      name = '';
      message = t('passkeys.added');
      await load();
    } catch (error) {
      if (!askReauth(error, enrol)) {
        failed = true;
        message = t('passkeys.error');
      }
    } finally {
      busy = false;
    }
  }

  async function revoke(item: PasskeyItem): Promise<void> {
    failed = false;
    message = '';
    try {
      await passkeyRevoke({
        headers: headers(),
        path: { passkey_id: item.id },
        throwOnError: true
      });
      message = t('passkeys.revoked');
      await load();
    } catch (error) {
      if (askReauth(error, () => revoke(item))) return;
      failed = true;
      const problem = (error ?? {}) as { code?: string };
      message = problem.code === 'last_login_factor' ? t('passkeys.last_factor') : t('passkeys.error');
    }
  }
</script>

<svelte:head>
  <title>{t('passkeys.title')}</title>
</svelte:head>

<main>
  <h1>{t('passkeys.title')}</h1>

  {#if items.length === 0}
    <p>{t('passkeys.none')}</p>
  {:else}
    <ul>
      {#each items as item (item.id)}
        <li>
          <span>{item.name}</span>
          <button type="button" onclick={() => revoke(item)}>{t('passkeys.revoke')}</button>
        </li>
      {/each}
    </ul>
  {/if}

  {#if available}
    <form onsubmit={register}>
      <label for="passkey-name">{t('passkeys.name')}</label>
      <input id="passkey-name" bind:value={name} maxlength="64" required />
      <button type="submit" disabled={busy}>{t('passkeys.add')}</button>
    </form>
  {:else if reason}
    <p class="muted">{reason}</p>
  {/if}

  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
  {/if}

  {#if message}
    <p role={failed ? 'alert' : 'status'} class:err={failed}>{message}</p>
  {/if}
</main>

<style>
  main {
    max-width: 42rem;
    margin: 0 auto;
    padding: 3rem 1rem;
  }
  li {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.5rem 0;
  }
  form {
    display: grid;
    gap: 0.5rem;
    margin-top: 1.5rem;
  }
  .muted {
    color: var(--color-muted);
  }
  .err {
    color: var(--color-err);
  }
</style>
