<script lang="ts">
  // "Historique d'activité" and the blocklist (EF-INT-019), for the administrator alone. The
  // server refuses anyone else (403, shown, never an empty page). A block is lifted only by a
  // person, in two steps (the question, then the confirmation): nothing here unblocks in silence.
  // No identifier of a source, path, host or address is in what the server answers.
  import { onMount } from 'svelte';
  import { opsActivityHistory, opsBlocklist, opsUnblockChapter } from '#lib/api/index.ts';
  import type { BlockOut, HistoryEntryOut } from '#lib/api/types.gen.ts';
  import ProblemAlert, { refusalOf } from '#lib/ui/molecules/ProblemAlert.svelte';
  import { formatDateTime } from '#lib/i18n/format.ts';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { navItems } from '#lib/nav.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import ConfirmDialog from '#lib/ui/molecules/ConfirmDialog.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

  const PAGE = 50;

  let entries = $state<HistoryEntryOut[]>([]);
  let next = $state<number | null>(null);
  let blocks = $state<BlockOut[]>([]);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);
  let asking = $state<string | null>(null); // the block whose lifting is being confirmed
  let message = $state('');
  const asked = $derived(blocks.find((block) => block.id === asking) ?? null);

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
    const { error } = await opsUnblockChapter({ path: { block_id: block.id } });
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

  const activityHref = navItems(['activity'])[0]?.href;
</script>

<svelte:head>
  <title>{t('history.title')}</title>
</svelte:head>

<main class="page">
  <PageHead
    title={t('history.title')}
    crumbs={[{ label: t('nav.activity'), href: activityHref }, { label: t('history.title') }]}
  />
  {#if problem}
    <ProblemAlert {problem} kind={refusalOf(problem)} />
  {:else if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('history.loading')}</p>
      <Skeleton shape="block" /><Skeleton /><Skeleton short />
    </div>
  {:else}
    <section class="section" aria-labelledby="h-events">
      <div class="section-head"><h2 id="h-events">{t('history.events')}</h2></div>
      {#if entries.length === 0}
        <EmptyState title={t('history.none')} art={false} headingLevel={3} />
      {:else}
        <ul class="entries">
          {#each entries as entry (entry.event_id)}
            <li class="event">
              <code class="code">{entry.type}</code>
              {#if entry.title}<span class="title">{entry.title}</span>{/if}
              <time class="folio" datetime={entry.occurred_at}>{formatDateTime(entry.occurred_at)}</time>
            </li>
          {/each}
        </ul>
        {#if next !== null}
          <p class="more"><Button onclick={() => loadHistory(next ?? undefined)}>{t('history.older')}</Button></p>
        {/if}
      {/if}
    </section>

    <section class="section" aria-labelledby="h-blocks">
      <div class="section-head"><h2 id="h-blocks">{t('history.blocked')}</h2></div>
      {#if message}<Alert tone="success" title={message} live="polite" />{/if}
      {#if blocks.length === 0}
        <EmptyState title={t('history.blocked_none')} art={false} headingLevel={3} />
      {:else}
        <ul class="entries">
          {#each blocks as block (block.id)}
            <li class="block">
              <div class="block__text">
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
              </div>
              <Button size="sm" onclick={() => (asking = block.id)}>{t('history.unblock')}</Button>
            </li>
          {/each}
        </ul>
      {/if}
    </section>
  {/if}
</main>

<ConfirmDialog
  bind:open={() => asking !== null, (value) => { if (!value) asking = null; }}
  title={t('history.unblock_title', { chapter: asked?.title ?? t('history.unknown_chapter') })}
  message={t('history.unblock_question')}
  confirmLabel={t('history.unblock_confirm')}
  cancelLabel={t('history.cancel')}
  tone="primary"
  onconfirm={() => (asked ? lift(asked) : undefined)}
/>

<style>
  .event,
  .block {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-1) var(--space-3);
    padding: var(--space-3) 0;
  }
  .block {
    align-items: center;
    justify-content: space-between;
  }
  .block__text {
    display: grid;
    min-width: 0;
  }
  .code {
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: var(--step--1);
    overflow-wrap: anywhere;
  }
  .title {
    font-weight: 600;
    overflow-wrap: anywhere;
  }
  .event .folio {
    margin-inline-start: auto;
  }
  .meta {
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  .more {
    margin: var(--space-4) 0 0;
  }
</style>
