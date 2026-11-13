<script lang="ts">
  import { onMount } from 'svelte';
  import { listActivity } from '#lib/api/index.ts';
  import type { ActivityOut } from '#lib/api/types.gen.ts';
  import ActivityItem from '#lib/components/ActivityItem.svelte';
  import { t } from '#lib/i18n/index.ts';

  let items = $state<ActivityOut[]>([]);
  let loaded = $state(false);

  async function load(): Promise<void> {
    const { data } = await listActivity({ throwOnError: true });
    items = data ?? [];
    loaded = true;
  }

  onMount(load);
</script>

<svelte:head>
  <title>{t('nav.activity')}</title>
</svelte:head>

<main>
  <h1>{t('nav.activity')}</h1>
  {#if !loaded}
    <p>Chargement…</p>
  {:else if items.length === 0}
    <p>Aucune activité.</p>
  {:else}
    <ul>
      {#each items as item (item.event_id)}
        <li><ActivityItem item={item} /></li>
      {/each}
    </ul>
  {/if}
</main>