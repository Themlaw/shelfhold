<script lang="ts">
  import { onMount } from 'svelte';
  import {
    passkeyRegister,
    passkeyRegistrationOptions,
    passkeyRevoke,
    passkeysList
  } from '#lib/api/index.ts';
  import type { PasskeyItem } from '#lib/api/types.gen.ts';
  import { t } from '#lib/i18n/index.ts';
  import { createPasskey, csrfToken, passkeysSupported } from '#lib/passkeys.ts';

  let items = $state<PasskeyItem[]>([]);
  let available = $state(false);
  let reason = $state<string | null>(null);
  let name = $state('');
  let message = $state('');
  let failed = $state(false);
  let busy = $state(false);

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

  async function register(event: SubmitEvent): Promise<void> {
    event.preventDefault();
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
    } catch {
      failed = true;
      message = t('passkeys.error');
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
