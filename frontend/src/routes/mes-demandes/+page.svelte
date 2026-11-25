<script lang="ts">
  import { onMount } from 'svelte';
  import { cancel, mine } from '#lib/api/index.ts';
  import type { RequestOut } from '#lib/api/types.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import RequestItem from '#lib/components/RequestItem.svelte';
  import { formatNumber, t } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import { reloadOnStream } from '#lib/live.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  let items = $state<RequestOut[]>([]);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);
  let cancelling = $state<string | null>(null);
  let cancelProblem = $state<ProblemView | null>(null);

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
    cancelProblem = null;
    try {
      const { error } = await cancel({
        headers: { 'X-CSRF-Token': csrfToken() },
        path: { request_id: id }
      });
      if (error !== undefined) {
        cancelProblem = problemView(error);
      } else {
        await load();
      }
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

<main class="page page--read">
  <div class="running-head">
    <span>{t('home.my_requests')}</span>
    {#if loaded && !problem && items.length > 0}<span class="running-head__folio">{formatNumber(items.length)}</span>{/if}
  </div>
  <div class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{t('nav.requests')}</h1>
    </div>
  </div>

  {#if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('home.loading')}</p>
      <Skeleton /><Skeleton /><Skeleton short />
    </div>
  {:else if problem}
    <ProblemCard {problem} />
  {:else if items.length === 0}
    <EmptyState title={t('home.requests_empty')}>
      {#snippet actions()}<Button href="/chercher" variant="primary">{t('home.search_book')}</Button>{/snippet}
    </EmptyState>
  {:else}
    {#if cancelProblem}
      <ProblemCard problem={cancelProblem} />
    {/if}
    <ul class="entries" role="list">
      {#each items as request (request.id)}
        <li>
          <RequestItem {request}>
            {#snippet actions()}
              {#if request.state === 'pending' || request.state === 'approved'}
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={cancelling === request.id}
                  loading={cancelling === request.id}
                  onclick={() => cancelOne(request.id)}
                >
                  {t('request.cancel')}
                </Button>
              {/if}
            {/snippet}
          </RequestItem>
        </li>
      {/each}
    </ul>
  {/if}
</main>
