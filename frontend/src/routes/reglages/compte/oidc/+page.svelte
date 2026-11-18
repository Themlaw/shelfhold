<script lang="ts">
  import { onMount } from 'svelte';
  import { oidcIdentities, oidcLink, oidcUnlink } from '#lib/api/index.ts';
  import type { OidcIdentityItem } from '#lib/api/types.gen.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { formatDate } from '#lib/i18n/format.ts';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { needsReauth, problemOf } from '#lib/triage.ts';

  const INTENT = 'oidc-intent';
  type Intent = 'link' | 'unlink';

  let items = $state<OidcIdentityItem[]>([]);
  let issuer = $state<string | null>(null);
  let loaded = $state(false);
  let message = $state('');
  let failed = $state(false);
  let busy = $state(false);
  let reauthMethods = $state<string[] | null>(null);
  let pending: Intent | null = null;

  const params = typeof window === 'undefined' ? null : new URLSearchParams(window.location.search);

  async function load(): Promise<void> {
    try {
      const { data } = await oidcIdentities({ throwOnError: true });
      items = data.items;
      issuer = data.configured_issuer;
    } catch {
      failed = true;
      message = t('oidc.list_error');
    } finally {
      loaded = true;
    }
  }

  /** What was asked before the provider took the browser away: taken up once it is back. */
  const takeIntent = (): Intent | null => {
    const value = sessionStorage.getItem(INTENT);
    sessionStorage.removeItem(INTENT);
    return value === 'link' || value === 'unlink' ? value : null;
  };

  onMount(() => {
    // the confirmation page of a link, and the provider's answer to a proof, send the person back
    // here with the result in the address
    const result = params?.get('oidc');
    const intent = takeIntent();
    if (result === 'linked') message = t('oidc.linked');
    if (result === 'reauth') message = t('oidc.reauth_done');
    if (result === 'refused') {
      failed = true;
      message = t('oidc.refused');
    }
    // only a proof the provider gave is followed by what the person had asked for
    const resume = result === 'reauth' && intent ? run(intent) : Promise.resolve();
    return resume.then(load);
  });

  /** A stale session asks for a proof in place (EF-USR-022); a session the provider opened is
   * sent back to it (`prompt=login`, `max_age`) and the action is taken up on return. */
  async function run(intent: Intent): Promise<void> {
    busy = true;
    failed = false;
    message = '';
    pending = intent;
    try {
      if (intent === 'link') {
        const { data } = await oidcLink({
          headers: { 'X-CSRF-Token': csrfToken() },
          throwOnError: true
        });
        window.location.assign(data.authorization_url);
      } else {
        await oidcUnlink({ headers: { 'X-CSRF-Token': csrfToken() }, throwOnError: true });
        message = t('oidc.unlinked');
        await load();
      }
      reauthMethods = null;
    } catch (error) {
      if (needsReauth(error)) {
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else {
        failed = true;
        message = t(intent === 'link' ? 'oidc.error' : 'oidc.unlink_error');
      }
    } finally {
      busy = false;
    }
  }

  const link = () => run('link');
  const unlink = () => run('unlink');

  async function afterReauth(): Promise<void> {
    reauthMethods = null;
    if (pending) await run(pending);
  }
</script>

<svelte:head>
  <title>{t('oidc.title')}</title>
</svelte:head>

<main>
  <h1>{t('oidc.title')}</h1>
  <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
  <p><a href="/reglages">{t('oidc.back')}</a></p>

  {#if loaded && issuer === null && items.length === 0}
    <p>{t('oidc.not_configured')}</p>
  {:else if issuer !== null}
    <p>{t('oidc.issuer', { issuer })}</p>
  {/if}

  {#if issuer === null && items.length === 0}
    <!-- nothing to link, nothing to remove -->
  {:else if items.length === 0}
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
          <button type="button" disabled={busy} onclick={unlink}>{t('oidc.unlink')}</button>
        </li>
      {/each}
    </ul>
  {/if}

  {#if items.length === 0 && issuer !== null}
    <button type="button" disabled={busy} onclick={link}>{t('oidc.link')}</button>
  {/if}

  {#if reauthMethods}
    <ReauthPrompt
      methods={reauthMethods}
      ondone={afterReauth}
      oncancel={() => (reauthMethods = null)}
      onprovider={() => sessionStorage.setItem(INTENT, pending ?? 'link')}
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
