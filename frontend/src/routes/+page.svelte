<script lang="ts">
  import { onMount } from 'svelte';
  import { listActivity } from '#lib/api/index.ts';
  import type { ActivityOut } from '#lib/api/types.gen.ts';
  import ActivityItem from '#lib/components/ActivityItem.svelte';
  import EmptyState from '#lib/components/EmptyState.svelte';
  import HealthStatus from '#lib/components/HealthStatus.svelte';
  import { t } from '#lib/i18n/index.ts';

  let items = $state<ActivityOut[]>([]);
  let loaded = $state(false);

  async function load(): Promise<void> {
    try {
      const { data } = await listActivity({ throwOnError: true });
      items = data ?? [];
    } catch {
      items = [];
    }
    loaded = true;
  }

  onMount(load);
</script>

<svelte:head>
  <title>{t('home.title')}</title>
</svelte:head>

<main>
  <h1>{t('home.title')}</h1>
  <p class="tagline">{t('home.tagline')}</p>
  <HealthStatus />

  <section class="recently-arrived">
    <h2>{t('home.recently_arrived')}</h2>
    {#if !loaded}
      <p>Chargement…</p>
    {:else if items.length === 0}
      <EmptyState title={t('home.empty_library')} actionLabel={t('home.add_first_book')} href="/assistant" />
    {:else}
      <ul>
        {#each items as item (item.event_id)}
          <li><ActivityItem item={item} /></li>
        {/each}
      </ul>
    {/if}
  </section>
</main>

<style>
  main {
    max-width: 42rem;
    margin: 0 auto;
    padding: 3rem 1rem;
  }
  h1 {
    font-family: var(--font-display);
    font-weight: 400;
    font-style: italic;
    letter-spacing: -0.02em;
    margin: 0 0 0.5rem;
  }
  .tagline {
    color: var(--color-muted);
    margin: 0 0 2rem;
  }
  .recently-arrived h2 {
    font-size: 1.125rem;
    margin: 2rem 0 0.5rem;
  }
  .recently-arrived ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
</style>