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

  const WINDOWS = [7, 30, 90];

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

<main>
  <h1>{t('storage.title')}</h1>
  {#if problem}
    <ProblemCard {problem} />
  {:else if board}
    <section aria-labelledby="h-space">
      <h2 id="h-space">{t('storage.space')}</h2>
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">{t('storage.shelf')}</th>
              <th scope="col" class="num">{t('storage.size')}</th>
              <th scope="col" class="num">{t('storage.copies')}</th>
              <th scope="col" class="num">{t('storage.in_store')}</th>
              <th scope="col" class="num">{t('storage.in_library')}</th>
            </tr>
          </thead>
          <tbody>
            {#each board.shelves as shelf (shelf.shelf_id ?? 'none')}
              <tr>
                <th scope="row">{shelf.name ?? t('storage.unshelved')}</th>
                <td class="num">{formatBytes(shelf.bytes)}</td>
                <td class="num">{formatNumber(shelf.copies)}</td>
                <td class="num">{formatBytes(shelf.by_location.store ?? 0)}</td>
                <td class="num">{formatBytes(shelf.by_location.library ?? 0)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <section aria-labelledby="h-traffic">
      <h2 id="h-traffic">{t('storage.traffic')}</h2>
      <p class="note">{t('storage.traffic_note')}</p>
      <p>
        <label for="window">{t('storage.window')}</label>
        <select id="window" bind:value={days} onchange={load}>
          {#each WINDOWS as value (value)}
            <option {value}>{t('storage.window_days', { count: value })}</option>
          {/each}
        </select>
      </p>
      {#if board.traffic.sources.length === 0}
        <p>{t('storage.traffic_none')}</p>
      {:else}
        {#each board.traffic.sources as source (source.source_key)}
          <h3>{source.source_key}</h3>
          <p>
            {t('storage.source_total', {
              requests: source.requests,
              size: formatBytes(source.bytes_in)
            })}
          </p>
          <div class="scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t('storage.day')}</th>
                  <th scope="col" class="num">{t('storage.requests')}</th>
                  <th scope="col" class="num">{t('storage.received')}</th>
                </tr>
              </thead>
              <tbody>
                {#each source.by_day as day (day.day)}
                  <tr>
                    <th scope="row">{formatDate(day.day)}</th>
                    <td class="num">{formatNumber(day.requests)}</td>
                    <td class="num">{formatBytes(day.bytes_in)}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/each}
      {/if}
    </section>
  {/if}
</main>

<style>
  main {
    max-width: 56rem;
    margin: 0 auto;
    padding: 1rem;
  }
  .scroll {
    overflow-x: auto;
  }
  table {
    border-collapse: collapse;
    width: 100%;
  }
  th,
  td {
    padding: 0.4rem 0.6rem;
    border-bottom: 1px solid var(--color-rule);
    text-align: start;
  }
  .num {
    text-align: end;
    font-variant-numeric: tabular-nums;
  }
  .note {
    color: var(--color-muted);
  }
</style>
