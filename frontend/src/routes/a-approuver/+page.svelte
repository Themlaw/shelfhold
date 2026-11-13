<script lang="ts">
  import { onMount } from 'svelte';
  import { approval, queue as loadQueue, reject, notFound } from '#lib/api/index.ts';
  import type { ApprovalOut, QueueItemOut } from '#lib/api/types.gen.ts';
  import ApproveSheet from '#lib/components/ApproveSheet.svelte';
  import EmptyState from '#lib/components/EmptyState.svelte';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  let items = $state<QueueItemOut[]>([]);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);
  let selected = $state<QueueItemOut | null>(null);
  let preview = $state<ApprovalOut | null>(null);
  let reason = $state('');

  async function load(): Promise<void> {
    const { data, error } = await loadQueue();
    if (error !== undefined) {
      problem = problemView(error);
    } else {
      problem = null;
      items = data?.items ?? [];
    }
    loaded = true;
  }

  async function open(item: QueueItemOut): Promise<void> {
    selected = item;
    preview = null;
    const { data } = await approval({ path: { request_id: item.id } });
    preview = data ?? null;
  }

  async function doReject(): Promise<void> {
    if (!selected) return;
    await reject({
      headers: { 'X-CSRF-Token': csrfToken() },
      path: { request_id: selected.id },
      body: { reason }
    });
    selected = null;
    preview = null;
    reason = '';
    await load();
  }

  async function doNotFound(): Promise<void> {
    if (!selected) return;
    await notFound({
      headers: { 'X-CSRF-Token': csrfToken() },
      path: { request_id: selected.id }
    });
    selected = null;
    preview = null;
    await load();
  }

  onMount(load);
</script>

<svelte:head>
  <title>{t('approve.title')}</title>
</svelte:head>

<main>
  <h1>{t('approve.title')}</h1>

  {#if !loaded}
    <p>{t('home.loading')}</p>
  {:else if problem}
    <ProblemCard {problem} />
  {:else if items.length === 0}
    <EmptyState title={t('home.requests_empty')} actionLabel={t('home.search_book')} href="/chercher" />
  {:else}
    <ul>
      {#each items as item (item.id)}
        <li>
          <button type="button" onclick={() => open(item)}>
            {item.title ?? item.id}
          </button>
          <span>{item.requester_role}</span>
          {#if item.needs_age}<span>{t('approve.choose_age')}</span>{/if}
        </li>
      {/each}
    </ul>
  {/if}

  {#if selected}
    <ApproveSheet
      item={selected}
      preview={preview}
      onApproved={async () => {
        selected = null;
        preview = null;
        await load();
      }}
    />
    <div class="reject">
      <input bind:value={reason} placeholder={t('approve.choose_age')} />
      <button type="button" onclick={doReject}>Refuser</button>
      <button type="button" onclick={doNotFound}>Introuvable</button>
    </div>
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
  li {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--color-rule, #eee);
  }
  .reject {
    display: flex;
    gap: 0.5rem;
    margin-top: 1rem;
  }
</style>
