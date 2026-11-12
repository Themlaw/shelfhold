<script lang="ts">
  import { onMount } from 'svelte';
  import { navByRole, type NavItem } from '#lib/nav.ts';

  let {
    role = null as string | null,
    items = null as NavItem[] | null,
    currentPath = null as string | null,
    reduced = false,
    options = undefined as unknown
  } = $props();

  let resolvedRole = $state<string | null>(role);
  let fetchedItems = $state<NavItem[] | null>(null);

  onMount(async () => {
    if (role === null && items === null) {
      try {
        const response = await fetch('/api/v1/me');
        if (response.ok) {
          const data = (await response.json()) as { role?: string; roles?: string[] };
          resolvedRole = data.role ?? data.roles?.[0] ?? null;
        }
      } catch {
        resolvedRole = null;
      }
    }
  });

  const resolvedItems = $derived(
    items !== null
      ? items
      : navByRole(resolvedRole, reduced ? true : options)
  );
</script>

<nav aria-label="Navigation">
  <ul>
    {#each resolvedItems as item (item.id)}
      <li>
        <a href={item.href} aria-current={currentPath === item.href ? 'page' : undefined}>
          {item.label}
        </a>
      </li>
    {/each}
  </ul>
</nav>

<style>
  nav ul {
    display: flex;
    gap: 0.5rem;
    list-style: none;
    margin: 0;
    padding: 0;
  }
  a {
    color: var(--color-accent);
    text-decoration: none;
  }
  a[aria-current='page'] {
    font-weight: 600;
  }
</style>