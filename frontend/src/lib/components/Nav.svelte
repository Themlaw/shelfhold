<script lang="ts">
  // EF-UX-005: the tabs the server offered to the signed-in role (see `nav.ts`).
  import { goto } from '$app/navigation';
  import { logout } from '#lib/api/index.ts';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { wipeOfflineCopies } from '#lib/offline/runtime.ts';
  import { navItems } from '#lib/nav.ts';

  let { ids, current = null }: { ids: readonly string[]; current?: string | null } = $props();

  const items = $derived(navItems(ids));

  let busy = $state(false);
  let failed = $state(false);

  async function signOut(): Promise<void> {
    busy = true;
    failed = false;
    // before the request: signing out offline, or a request that fails, still leaves nothing of the profile
    await wipeOfflineCopies();
    try {
      await logout({ headers: { 'X-CSRF-Token': csrfToken() }, throwOnError: true });
      // eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin
      await goto('/login');
    } catch {
      failed = true;
    } finally {
      busy = false;
    }
  }
</script>

{#if items.length > 0}
  <nav aria-label={t('nav.label')}>
    <ul>
      {#each items as item (item.id)}
        <li>
          <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
          <a href={item.href} aria-current={current === item.href ? 'page' : undefined}>
            {t(item.labelKey)}
          </a>
        </li>
      {/each}
      <li>
        <button type="button" disabled={busy} onclick={signOut}>{t('nav.logout')}</button>
      </li>
    </ul>
    {#if failed}
      <p role="alert" class="err">{t('nav.logout_failed')}</p>
    {/if}
  </nav>
{/if}

<style>
  nav {
    max-width: 42rem;
    margin: 0 auto;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--color-rule);
  }
  ul {
    display: flex;
    flex-wrap: wrap;
    gap: 1rem;
    list-style: none;
    margin: 0;
    padding: 0;
    align-items: center;
  }
  a {
    color: var(--color-accent);
    text-decoration: none;
    min-height: 2.75rem;
    min-width: 2.75rem;
    display: inline-flex;
    align-items: center;
  }
  a[aria-current='page'] {
    font-weight: 600;
  }
  button {
    min-height: 2.75rem;
    min-width: 2.75rem;
    display: inline-flex;
    align-items: center;
  }
  .err {
    color: var(--color-err);
    margin: 0.5rem 0 0;
  }
</style>