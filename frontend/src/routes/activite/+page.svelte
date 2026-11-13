<script lang="ts">
  // The activity page (EF-UX-010): what happened, as this reader may read it. Everything comes
  // from `GET /api/v1/activity`, already filtered and already worded by the server: one state for
  // the family, the steps and `koreader_reset` for the admin. It follows the one event stream.
  import { onMount } from 'svelte';
  import { listActivity } from '#lib/api/index.ts';
  import type { ActivityOut } from '#lib/api/types.gen.ts';
  import ActivityItem from '#lib/components/ActivityItem.svelte';
  import EmptyState from '#lib/components/EmptyState.svelte';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { reloadOnStream } from '#lib/live.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  let items = $state<ActivityOut[]>([]);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);

  async function load(): Promise<void> {
    const { data, error } = await listActivity();
    if (error !== undefined) {
      problem = problemView(error);
    } else {
      problem = null;
      items = data ?? [];
    }
    loaded = true;
  }

  onMount(() => {
    void load();
    return reloadOnStream(load);
  });
</script>

<svelte:head>
  <title>{t('nav.activity')}</title>
</svelte:head>

<main>
  <h1>{t('nav.activity')}</h1>
  {#if !loaded}
    <p>{t('activity.loading')}</p>
  {:else if problem}
    <ProblemCard {problem} />
  {:else if items.length === 0}
    <EmptyState title={t('activity.empty')} actionLabel={t('home.search_book')} href="/chercher" />
  {:else}
    <ul>
      {#each items as item (item.event_id)}
        <li><ActivityItem {item} /></li>
      {/each}
    </ul>
  {/if}
</main>

<style>
  main {
    max-width: 42rem;
    margin: 0 auto;
    padding: 1rem;
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
</style>
