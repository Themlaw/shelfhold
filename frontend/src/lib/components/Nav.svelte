<script lang="ts">
  // EF-UX-005: the tabs the server offered to the signed-in role (see `nav.ts`).
  import { navItems } from '#lib/nav.ts';
  import { t } from '#lib/i18n/index.ts';

  let { ids, current = null }: { ids: readonly string[]; current?: string | null } = $props();

  const items = $derived(navItems(ids));
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
    </ul>
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
</style>
