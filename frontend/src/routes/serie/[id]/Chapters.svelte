<script lang="ts">
  // The chapters of an Edition, in reading order (EF-BIB-005): one page of the units the server
  // lists, each a row of the table of contents. A readable unit opens in the reader; the read
  // state (read, in progress) is shown when the server gives one, and nothing is made up for the
  // others.
  import { editionUnits } from '#lib/api/index.ts';
  import type { EditionUnitOut } from '#lib/api/types.gen.ts';
  import { t } from '#lib/i18n/index.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import ChapterRow from '#lib/ui/molecules/ChapterRow.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import ErrorState from '#lib/ui/molecules/ErrorState.svelte';
  import Pagination from '#lib/ui/molecules/Pagination.svelte';

  const PAGE_SIZE = 50;

  let { editionId }: { editionId: string } = $props();

  let items = $state<EditionUnitOut[]>([]);
  let total = $state(0);
  let page = $state(1);
  let loading = $state(true);
  let problem = $state<ProblemView | null>(null);

  const pages = $derived(Math.max(1, Math.ceil(total / PAGE_SIZE)));

  async function load(target: number): Promise<void> {
    loading = true;
    const { data, error } = await editionUnits({
      path: { edition_id: editionId },
      query: { limit: PAGE_SIZE, offset: (target - 1) * PAGE_SIZE }
    });
    loading = false;
    if (error !== undefined || !data) {
      problem = problemView(error);
      return;
    }
    problem = null;
    items = data.items;
    total = data.total;
    page = target;
  }

  const rowState = (unit: EditionUnitOut) =>
    !unit.readable ? 'locked' : unit.read_state === 'read' ? 'read' : unit.read_state === 'in_progress' ? 'current' : undefined;

  $effect(() => {
    void editionId;
    void load(1);
  });
</script>

<section class="section" aria-labelledby="chapters-title">
  <div class="section-head">
    <h2 id="chapters-title">{t('chapters.title')}</h2>
    {#if !problem && total > 0}<span class="folio">{t('chapters.count', { count: total })}</span>{/if}
  </div>
  {#if problem}
    <ErrorState kind="server" title={t('chapters.error_title')} headingLevel={2}>
      {t('chapters.error')}
      {#snippet actions()}<Button icon="refresh" onclick={() => void load(page)}>{t('error.retry')}</Button>{/snippet}
    </ErrorState>
  {:else if loading && items.length === 0}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('chapters.loading')}</p>
      <Skeleton /><Skeleton /><Skeleton short />
    </div>
  {:else if items.length === 0}
    <EmptyState title={t('chapters.empty')} headingLevel={3} art={false} />
  {:else}
    <ol class="toc" aria-busy={loading} aria-label={t('chapters.title')}>
      {#each items as unit (unit.id)}
        <ChapterRow
          number={unit.number}
          title={unit.title ?? t('chapters.untitled', { number: unit.number })}
          href={unit.readable ? `/lire/unit/${unit.id}` : null}
          state={rowState(unit)}
        >
          {#snippet end()}
            {#if unit.read_state === 'read'}{t('chapters.read')}{:else if unit.read_state === 'in_progress'}{t('chapters.in_progress')}{/if}
          {/snippet}
        </ChapterRow>
      {/each}
    </ol>
    {#if pages > 1}
      <Pagination {page} {pages} label={t('chapters.pagination')} onpage={(target) => void load(target)} />
    {/if}
  {/if}
</section>
