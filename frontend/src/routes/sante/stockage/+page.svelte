<script lang="ts">
  // "Stockage et réseau" (EF-INT-020): the space each shelf holds and what each source made go
  // through the network, for the administrator alone. The server refuses anyone else (403, shown,
  // never an empty page). Sizes and counts only: no path, host, address or key is in the answer.
  import { onMount } from 'svelte';
  import { opsStorageDashboard } from '#lib/api/index.ts';
  import type { StorageOut } from '#lib/api/types.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { formatBytes } from '#lib/bytes.ts';
  import { formatDate, formatNumber } from '#lib/i18n/format.ts';
  import { t } from '#lib/i18n/index.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import DataTable from '#lib/ui/organisms/DataTable.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

  const WINDOWS = [7, 30, 90];
  const SPACE_COLUMNS = [
    { key: 'name', label: t('storage.shelf'), primary: true },
    { key: 'bytes', label: t('storage.size'), num: true },
    { key: 'copies', label: t('storage.copies'), num: true },
    { key: 'store', label: t('storage.in_store'), num: true },
    { key: 'library', label: t('storage.in_library'), num: true }
  ] as const;
  const DAY_COLUMNS = [
    { key: 'day', label: t('storage.day'), primary: true },
    { key: 'requests', label: t('storage.requests'), num: true },
    { key: 'bytes_in', label: t('storage.received'), num: true }
  ] as const;

  let board = $state<StorageOut | null>(null);
  let problem = $state<ProblemView | null>(null);
  let days = $state(30);

  async function load(): Promise<void> {
    const { data, error } = await opsStorageDashboard({ query: { days } });
    if (error !== undefined || data === undefined) {
      problem = problemView(error);
      board = null;
      return;
    }
    problem = null;
    board = data;
  }

  onMount(load);
</script>

<svelte:head>
  <title>{t('storage.title')}</title>
</svelte:head>

<main class="page">
  <PageHead
    title={t('storage.title')}
    crumbs={[
      { label: t('health_page.crumb'), href: '/reglages' },
      { label: t('health_page.title'), href: '/sante' },
      { label: t('storage.title') }
    ]}
  />
  {#if problem}
    <ProblemCard {problem} />
  {:else if !board}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('storage.loading')}</p>
      <Skeleton shape="block" /><Skeleton /><Skeleton short />
    </div>
  {:else}
    <section class="section" aria-labelledby="h-space">
      <div class="section-head"><h2 id="h-space">{t('storage.space')}</h2></div>
      <DataTable
        caption={t('storage.space')}
        captionHidden
        columns={SPACE_COLUMNS}
        rows={board.shelves.map((shelf) => ({
          id: shelf.shelf_id ?? 'none',
          name: shelf.name ?? t('storage.unshelved'),
          bytes: formatBytes(shelf.bytes),
          copies: formatNumber(shelf.copies),
          store: formatBytes(shelf.by_location.store ?? 0),
          library: formatBytes(shelf.by_location.library ?? 0)
        }))}
        key={(row) => String(row.id)}
      />
    </section>

    <section class="section" aria-labelledby="h-traffic">
      <div class="section-head">
        <h2 id="h-traffic">{t('storage.traffic')}</h2>
      </div>
      <p class="muted note">{t('storage.traffic_note')}</p>
      <div class="window">
        <Field label={t('storage.window')}>
          {#snippet children(control)}
            <Select
              {...control}
              inline
              value={String(days)}
              onchange={(event: Event) => {
                days = Number((event.currentTarget as HTMLSelectElement).value);
                void load();
              }}
              options={WINDOWS.map((value) => ({ value: String(value), label: t('storage.window_days', { count: value }) }))}
            />
          {/snippet}
        </Field>
      </div>
      {#if board.traffic.sources.length === 0}
        <p class="muted">{t('storage.traffic_none')}</p>
      {:else}
        {#each board.traffic.sources as source (source.source_key)}
          <h3 class="source">{source.source_key}</h3>
          <p class="num">
            {t('storage.source_total', {
              requests: source.requests,
              size: formatBytes(source.bytes_in)
            })}
          </p>
          <DataTable
            caption={source.source_key}
            captionHidden
            compact
            columns={DAY_COLUMNS}
            rows={source.by_day.map((day) => ({
              day: formatDate(day.day),
              requests: formatNumber(day.requests),
              bytes_in: formatBytes(day.bytes_in)
            }))}
            key={(row) => String(row.day)}
          />
        {/each}
      {/if}
    </section>
  {/if}
</main>

<style>
  .note {
    max-width: var(--measure);
  }
  .window {
    margin: var(--space-4) 0;
  }
  .source {
    margin: var(--space-6) 0 var(--space-1);
    font-size: var(--step-1);
    overflow-wrap: anywhere;
  }
</style>
