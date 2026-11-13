<script lang="ts">
  // The home page (EF-UX-005): "Arrivé récemment" and "My requests". Both lists are the server's,
  // filtered for the reader on the server (a child is shown no Work that is hidden from her): the
  // screen shows what it is given, and an empty answer is one state whatever the reason.
  import { onMount } from 'svelte';
  import { listWorks, mine as myRequests } from '#lib/api/index.ts';
  import type { RequestOut, WorkItemOut } from '#lib/api/types.gen.ts';
  import EmptyState from '#lib/components/EmptyState.svelte';
  import HealthStatus from '#lib/components/HealthStatus.svelte';
  import RequestItem from '#lib/components/RequestItem.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { reloadOnStream } from '#lib/live.ts';

  const RECENT = 12;

  let works = $state<WorkItemOut[]>([]);
  let requests = $state<RequestOut[]>([]);
  let loaded = $state(false);

  async function load(): Promise<void> {
    const [recent, own] = await Promise.all([
      listWorks({ query: { sort: 'recent', limit: RECENT } }),
      myRequests()
    ]);
    works = recent.data?.items ?? [];
    requests = own.data?.items ?? [];
    loaded = true;
  }

  onMount(() => {
    void load();
    return reloadOnStream(load);
  });
</script>


<svelte:head>
  <title>{t('home.title')}</title>
</svelte:head>

<main>
  <h1>{t('home.title')}</h1>
  <p class="tagline">{t('home.tagline')}</p>
  <HealthStatus />

  {#if !loaded}
    <p>{t('home.loading')}</p>
  {:else}
    <section class="recently-arrived" aria-labelledby="recent-title">
      <h2 id="recent-title">{t('home.recently_arrived')}</h2>
      {#if works.length === 0}
        <EmptyState
          title={t('home.empty_library')}
          actionLabel={t('home.add_first_book')}
          href="/chercher"
        />
      {:else}
        <ul>
          {#each works as work (work.work_id)}
            <li>{work.title}</li>
          {/each}
        </ul>
      {/if}
    </section>

    <section class="my-requests" aria-labelledby="requests-title">
      <h2 id="requests-title">{t('home.my_requests')}</h2>
      {#if requests.length === 0}
        <EmptyState
          title={t('home.requests_empty')}
          actionLabel={t('home.search_book')}
          href="/chercher"
        />
      {:else}
        <ul>
          {#each requests as request (request.id)}
            <li><RequestItem {request} /></li>
          {/each}
        </ul>
      {/if}
    </section>
  {/if}
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
  section h2 {
    font-size: 1.125rem;
    margin: 2rem 0 0.5rem;
  }
  section ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
</style>
