<script lang="ts">
  import { onMount } from 'svelte';
  import { cancel, mine } from '#lib/api/index.ts';
  import type { RequestOut } from '#lib/api/types.gen.ts';
  import EmptyState from '#lib/components/EmptyState.svelte';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import RequestItem from '#lib/components/RequestItem.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { reloadOnStream } from '#lib/live.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  let items = $state<RequestOut[]>([]);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);
  let cancelling = $state<string | null>(null);

  async function load(): Promise<void> {
    const { data, error } = await mine();
    if (error !== undefined) {
      problem = problemView(error);
    } else {
      problem = null;
      items = data?.items ?? [];
    }
    loaded = true;
  }

  async function cancelOne(id: string): Promise<void> {
    cancelling = id;
    try {
      await cancel({ path: { request_id: id } });
      await load();
    } finally {
      cancelling = null;
    }
  }

  onMount(() => {
    void load();
    return reloadOnStream(load);
  });
</script>

<svelte:head>
  <title>{t('nav.requests')}</title>
</svelte:head>

<main>
  <h1>{t('nav.requests')}</h1>

  {#if !loaded}
    <p>{t('home.loading')}</p>
  {:else if problem}
    <ProblemCard {problem} />
  {:else if items.length === 0}
    <EmptyState
      title={t('home.requests_empty')}
      actionLabel={t('home.search_book')}
      href="/chercher"
    />
  {:else}
    <ul>
      {#each items as request (request.id)}
        <li class="request-row">
          <RequestItem {request} />
          {#if request.state === 'pending' || request.state === 'approved'}
            <button
              type="button"
              disabled={cancelling === request.id}
              onclick={() => cancelOne(request.id)}
            >
              {t('request.cancel')}
            </button>
          {/if}
        </li>
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
  .request-row {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--color-rule, #eee);
  }
</style>
