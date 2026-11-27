<script lang="ts">
  // What an Edition has been exported as (EF-INT-007): the objects by kind, what a Kobo is offered
  // as KEPUB, the destinations (an e-reader, a shelf of another server) and the blocks made for
  // OPDS. The route is the admin's: the tab is not drawn for a member or a child, so it is not
  // asked for.
  import { editionExports } from '#lib/api/index.ts';
  import type { EditionExportsOut } from '#lib/api/types.gen.ts';
  import { formatBytes } from '#lib/bytes.ts';
  import { formatDate, formatNumber, t, type MessageKey } from '#lib/i18n/index.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import ErrorState from '#lib/ui/molecules/ErrorState.svelte';

  let { editionId }: { editionId: string } = $props();

  const KINDS = ['unit', 'volume', 'bundle', 'adopted', 'opds_bundle'] as const;

  let loaded = $state<EditionExportsOut | null>(null);
  let loading = $state(true);
  let problem = $state<ProblemView | null>(null);

  const empty = $derived(
    loaded !== null &&
      loaded.kepub_items === 0 &&
      loaded.destinations.length === 0 &&
      loaded.blocks.length === 0 &&
      KINDS.every((kind) => loaded!.items[kind] === 0)
  );

  async function load(): Promise<void> {
    loading = true;
    const { data, error } = await editionExports({ path: { edition_id: editionId } });
    loading = false;
    if (error !== undefined || !data) {
      problem = problemView(error);
      return;
    }
    problem = null;
    loaded = data;
  }

  $effect(() => {
    void editionId;
    void load();
  });
</script>

<section class="section" aria-labelledby="exports-title">
  <div class="section-head"><h2 id="exports-title">{t('exports.title')}</h2></div>
  {#if problem}
    <ErrorState kind="server" title={t('exports.error_title')} headingLevel={2}>
      {t('exports.error')}
      {#snippet actions()}<Button icon="refresh" onclick={() => void load()}>{t('error.retry')}</Button>{/snippet}
    </ErrorState>
  {:else if loading && !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('exports.loading')}</p>
      <Skeleton /><Skeleton /><Skeleton short />
    </div>
  {:else if loaded === null || empty}
    <EmptyState title={t('exports.empty')} headingLevel={3} art={false} />
  {:else}
    <dl class="exports__kinds">
      {#each KINDS as kind (kind)}
        <div>
          <dt>{t(`exports.kind.${kind}` as MessageKey)}</dt>
          <dd class="folio">{formatNumber(loaded.items[kind])}</dd>
        </div>
      {/each}
      <div>
        <dt>{t('exports.kepub')}</dt>
        <dd class="folio">{formatNumber(loaded.kepub_items)}</dd>
      </div>
    </dl>

    {#if loaded.destinations.length > 0}
      <h3 class="exports__sub">{t('exports.destinations')}</h3>
      <ul class="entries">
        {#each loaded.destinations as destination (destination.destination_id)}
          <li class="exports__row">
            <span class="exports__name">{destination.target_name} <span class="hint">· {destination.shelf_name}</span></span>
            {#if !destination.enabled}<Badge>{t('exports.disabled')}</Badge>{/if}
            <span class="hint">
              {t('exports.counts', {
                exported: destination.exported,
                pending: destination.pending,
                failed: destination.failed
              })}
              {#if destination.last_exported_at}
                · {t('exports.last', { date: formatDate(destination.last_exported_at) })}
              {/if}
            </span>
          </li>
        {/each}
      </ul>
    {/if}

    {#if loaded.blocks.length > 0}
      <h3 class="exports__sub">{t('exports.blocks')}</h3>
      <ul class="entries">
        {#each loaded.blocks as block (`${block.profile_id}-${block.part_index}`)}
          <li class="exports__row">
            <span class="exports__name">{block.username} <span class="hint">· {t('exports.part', { number: block.part_index + 1 })}</span></span>
            <span class="hint">
              {formatBytes(block.size)}
              {#if block.last_used_at}· {t('exports.used', { date: formatDate(block.last_used_at) })}{/if}
            </span>
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</section>

<style>
  .exports__kinds {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
    gap: var(--space-4);
    margin: 0;
  }
  .exports__kinds dt {
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  .exports__kinds dd {
    margin: 0;
    font-size: var(--step-2);
  }
  .exports__sub {
    margin: var(--space-6) 0 var(--space-2);
    font: 600 var(--step-1) / 1.2 var(--font-text);
  }
  .exports__row {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-2) var(--space-3);
    min-height: var(--target);
    padding: var(--space-2) 0;
  }
  .exports__name {
    flex: 1 1 12rem;
    min-width: 0;
    overflow-wrap: anywhere;
  }
</style>
