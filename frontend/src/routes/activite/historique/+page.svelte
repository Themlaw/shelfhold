<script lang="ts">
  // "Historique d'activité" and the blocklist (EF-INT-019), for the administrator alone. The
  // server refuses anyone else (403, shown, never an empty page). A block is lifted only by a
  // person, in two steps (the question, then the confirmation): nothing here unblocks in silence.
  // No identifier of a source, path, host or address is in what the server answers.
  import { onMount } from 'svelte';
  import { opsActivityHistory, opsBlocklist, opsUnblockChapter } from '#lib/api/index.ts';
  import type { BlockOut, HistoryEntryOut } from '#lib/api/types.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { formatDateTime } from '#lib/i18n/format.ts';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  const PAGE = 50;

  let entries = $state<HistoryEntryOut[]>([]);
  let next = $state<number | null>(null);
  let blocks = $state<BlockOut[]>([]);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);
  let asking = $state<string | null>(null); // the block whose lifting is being confirmed
  let message = $state('');
  let busy = $state(false);

  async function loadHistory(before?: number): Promise<void> {
    const { data, error } = await opsActivityHistory({ query: { limit: PAGE, before } });
    if (error !== undefined || data === undefined) {
      problem = problemView(error);
      return;
    }
    entries = before === undefined ? data.items : [...entries, ...data.items];
    next = data.next_before;
  }

  async function loadBlocks(): Promise<void> {
    const { data, error } = await opsBlocklist();
    if (error !== undefined || data === undefined) {
      problem ??= problemView(error);
      return;
    }
    blocks = data.items;
  }

  async function lift(block: BlockOut): Promise<void> {
    busy = true;
    const { error } = await opsUnblockChapter({ path: { block_id: block.id } });
    busy = false;
    asking = null;
    if (error !== undefined) {
      problem = problemView(error);
      return;
    }
    message = t('history.unblocked');
    await loadBlocks();
  }

  onMount(async () => {
    await loadHistory();
    if (problem === null) await loadBlocks();
    loaded = true;
  });
</script>

<svelte:head>
  <title>{t('history.title')}</title>
</svelte:head>

<main>
  <h1>{t('history.title')}</h1>
  {#if problem}
    <ProblemCard {problem} />
  {:else if loaded}
    <section aria-labelledby="h-events">
      <h2 id="h-events">{t('history.events')}</h2>
      {#if entries.length === 0}
        <p>{t('history.none')}</p>
      {:else}
        <ul>
          {#each entries as entry (entry.event_id)}
            <li>
              <code class="code">{entry.type}</code>
              <time datetime={entry.occurred_at}>{formatDateTime(entry.occurred_at)}</time>
              {#if entry.title}<span class="title">{entry.title}</span>{/if}
            </li>
          {/each}
        </ul>
        {#if next !== null}
          <button type="button" onclick={() => loadHistory(next ?? undefined)}>
            {t('history.older')}
          </button>
        {/if}
      {/if}
    </section>

    <section aria-labelledby="h-blocks">
      <h2 id="h-blocks">{t('history.blocked')}</h2>
      {#if message}<p role="status">{message}</p>{/if}
      {#if blocks.length === 0}
        <p>{t('history.blocked_none')}</p>
      {:else}
        <ul>
          {#each blocks as block (block.id)}
            <li>
              <span class="title">
                {#if block.title}
                  {block.title}{block.number ? ` — ${block.number}` : ''}
                {:else}
                  {t('history.unknown_chapter')}
                {/if}
              </span>
              <span class="meta">
                {block.source_key} · {t(`history.reason.${block.reason}` as MessageKey)} ·
                {t('history.reproposed', { count: block.proposed_count })}
              </span>
              {#if asking === block.id}
                <p>{t('history.unblock_question')}</p>
                <button type="button" disabled={busy} onclick={() => lift(block)}>
                  {t('history.unblock_confirm')}
                </button>
                <button type="button" onclick={() => (asking = null)}>{t('history.cancel')}</button>
              {:else}
                <button type="button" onclick={() => (asking = block.id)}>
                  {t('history.unblock')}
                </button>
              {/if}
            </li>
          {/each}
        </ul>
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
  ul {
    list-style: none;
    padding: 0;
  }
  li {
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--color-rule);
  }
  .code {
    font-family: monospace;
    overflow-wrap: anywhere;
    margin-inline-end: 0.5rem;
  }
  .title {
    font-weight: 600;
    margin-inline-end: 0.5rem;
  }
  .meta {
    color: var(--color-muted);
    display: block;
  }
</style>
