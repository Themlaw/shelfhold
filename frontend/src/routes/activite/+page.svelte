<script lang="ts">
  // The activity page (EF-UX-010): what happened, as this reader may read it. Everything comes
  // from `GET /api/v1/activity`, already filtered and already worded by the server: one state for
  // the family, the steps and `koreader_reset` for the admin. It follows the one event stream.
  import { onMount } from 'svelte';
  import { listActivity } from '#lib/api/index.ts';
  import type { ActivityOut } from '#lib/api/types.gen.ts';
  import ActivityItem from '#lib/components/ActivityItem.svelte';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { reloadOnStream } from '#lib/live.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

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

<main class="page page--read">
  <PageHead title={t('nav.activity')} />
  {#if !loaded}
    <div aria-busy="true">
      <p role="status">{t('activity.loading')}</p>
      <Skeleton /><Skeleton /><Skeleton short />
    </div>
  {:else if problem}
    <ProblemCard {problem} />
  {:else if items.length === 0}
    <EmptyState title={t('activity.empty')}>
      {#snippet actions()}
        <Button href="/chercher" icon="search">{t('home.search_book')}</Button>
      {/snippet}
    </EmptyState>
  {:else}
    <ul class="entries">
      {#each items as item (item.event_id)}
        <li><ActivityItem {item} /></li>
      {/each}
    </ul>
  {/if}
</main>
