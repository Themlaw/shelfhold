<script lang="ts">
  import { onMount } from 'svelte';
  import { oidcIdentities, oidcLink } from '#lib/api/index.ts';
  import type { OidcIdentityItem } from '#lib/api/types.gen.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { formatDate } from '#lib/i18n/format.ts';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { needsReauth, problemOf } from '#lib/triage.ts';

  let items = $state<OidcIdentityItem[]>([]);
  let message = $state('');
  let failed = $state(false);
  let busy = $state(false);
  let reauthMethods = $state<string[] | null>(null);

  const params = typeof window === 'undefined' ? null : new URLSearchParams(window.location.search);

  async function load(): Promise<void> {
    try {
      const { data } = await oidcIdentities({ throwOnError: true });
      items = data.items;
    } catch {
      failed = true;
      message = t('oidc.list_error');
    }
  }

  onMount(() => {
    // the confirmation page of a link sends the person back here with the result
    const result = params?.get('oidc');
    if (result === 'linked') message = t('oidc.linked');
    if (result === 'refused') {
      failed = true;
      message = t('oidc.refused');
    }
    return load();
  });

  /** A stale password asks for it in place (EF-USR-022), then starts the link again. */
  async function link(): Promise<void> {
    busy = true;
    failed = false;
    message = '';
    try {
      const { data } = await oidcLink({
        headers: { 'X-CSRF-Token': csrfToken() },
        throwOnError: true
      });
      window.location.assign(data.authorization_url);
    } catch (error) {
      if (needsReauth(error)) {
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else {
        failed = true;
        message = t('oidc.error');
      }
    } finally {
      busy = false;
    }
  }

  async function afterReauth(): Promise<void> {
    reauthMethods = null;
    await link();
  }
</script>

<svelte:head>
  <title>{t('oidc.title')}</title>
</svelte:head>

<main>
  <h1>{t('oidc.title')}</h1>

  {#if items.length === 0}
    <p>{t('oidc.none')}</p>
  {:else}
    <ul>
      {#each items as item (item.issuer)}
        <li>
          <span>{item.issuer}</span>
          <span class="muted">
            {t('oidc.linked_since', { date: formatDate(item.linked_at) })}
            {#if item.linked_by_other}· {t('oidc.by_admin')}{/if}
          </span>
        </li>
      {/each}
    </ul>
  {/if}

  {#if items.length === 0}
    <button type="button" disabled={busy} onclick={link}>{t('oidc.link')}</button>
  {/if}

  {#if reauthMethods}
    <ReauthPrompt
      methods={reauthMethods}
      ondone={afterReauth}
      oncancel={() => (reauthMethods = null)}
    />
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
  .muted {
    color: var(--color-muted);
  }
  .err {
    color: var(--color-err);
  }
</style>
