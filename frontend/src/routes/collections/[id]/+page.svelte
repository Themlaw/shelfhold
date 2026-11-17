<script lang="ts">
  // One collection or reading list (EF-BIB-016), in the order the server numbered it (from 1,
  // among the items the reader is shown). A list that is not hers is the same "not found" as one
  // nobody ever had. She changes her own lists; only an admin changes a household collection.
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import { deleteCollection, readCollection, removeItem } from '#lib/api/index.ts';
  import type { CollectionDetailOut } from '#lib/api/types.gen.ts';
  import { client } from '#lib/api/client.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  const id = $derived(page.params.id ?? '');
  let detail = $state<CollectionDetailOut | null>(null);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);
  let actionProblem = $state<ProblemView | null>(null);
  let isAdmin = $state(false);
  let busy = $state(false);
  let confirming = $state(false);

  const canWrite = $derived(!!detail && (detail.mine || (detail.scope === 'shared' && isAdmin)));
  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const thumb = (hash: string): string => `/api/v1/thumbnails/${hash}/160`;

  async function load(): Promise<void> {
    const { data, error } = await readCollection({ path: { collection_id: id } });
    if (error !== undefined || !data) {
      problem = problemView(error);
      detail = null;
    } else {
      problem = null;
      detail = data;
    }
    loaded = true;
  }

  async function remove(itemId: string): Promise<void> {
    busy = true;
    actionProblem = null;
    try {
      const { error } = await removeItem({
        headers: headers(),
        path: { collection_id: id, item_id: itemId }
      });
      if (error !== undefined) actionProblem = problemView(error);
      else await load();
    } finally {
      busy = false;
    }
  }

  async function destroy(): Promise<void> {
    busy = true;
    actionProblem = null;
    try {
      const { error } = await deleteCollection({ headers: headers(), path: { collection_id: id } });
      if (error !== undefined) {
        actionProblem = problemView(error);
        confirming = false;
      } else {
        // eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin
        await goto('/collections');
      }
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    void (async () => {
      const who = await client.get({ url: '/api/v1/me', throwOnError: false });
      isAdmin = !who.error && (who.data as { role?: string } | undefined)?.role === 'admin';
    })();
    void load();
  });
</script>

<svelte:head>
  <title>{detail ? (detail.name ?? t('collections.unnamed')) : t('collections.title')}</title>
</svelte:head>

<main>
  {#if !loaded}
    <h1>{t('collections.title')}</h1>
    <p>{t('collections.loading')}</p>
  {:else if problem || !detail}
    <h1>{t('collections.title')}</h1>
    {#if problem}<ProblemCard {problem} />{/if}
    <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
    <p><a href="/collections">{t('collections.back')}</a></p>
  {:else}
    <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
    <p><a href="/collections">{t('collections.back')}</a></p>
    <h1>{detail.name ?? t('collections.unnamed')}</h1>
    <p class="meta">
      {t(`collections.kind.${detail.kind}`)} · {t(`collections.scope.${detail.scope}`)} ·
      {t('collections.count', { count: detail.count })}
    </p>
    {#if actionProblem}
      <ProblemCard problem={actionProblem} />
    {/if}
    {#if detail.items.length === 0}
      <p>{t('collections.items_empty')}</p>
    {:else}
      <ol aria-label={t('collections.items')}>
        {#each detail.items as item (item.id)}
          <li>
            <span class="position">{item.position}</span>
            {#if item.cover_hash}
              <img src={thumb(item.cover_hash)} alt="" width="40" height="60" loading="lazy" />
            {/if}
            <span class="title">
              {item.title}
              {#if item.number}<span class="meta">{t('collections.number', { number: item.number })}</span>{/if}
            </span>
            {#if canWrite}
              <button
                type="button"
                disabled={busy}
                aria-label={t('collections.remove_item', { title: item.title })}
                onclick={() => remove(item.id)}
              >
                {t('collections.remove')}
              </button>
            {/if}
          </li>
        {/each}
      </ol>
    {/if}
    {#if canWrite}
      <div class="danger">
        {#if confirming}
          <button type="button" disabled={busy} onclick={destroy}>{t('collections.delete_confirm')}</button>
          <button type="button" disabled={busy} onclick={() => (confirming = false)}>{t('collections.delete_cancel')}</button>
        {:else}
          <button type="button" disabled={busy} onclick={() => (confirming = true)}>{t('collections.delete')}</button>
        {/if}
      </div>
    {/if}
  {/if}
</main>

<style>
  main { max-width: 42rem; margin: 0 auto; padding: 1rem; }
  ol { list-style: none; padding: 0; margin: 0 0 1rem; }
  li { display: flex; gap: 0.75rem; align-items: center; padding: 0.5rem 0; border-bottom: 1px solid var(--color-rule); }
  .position { min-width: 2ch; font-variant-numeric: tabular-nums; }
  .title { flex: 1; overflow-wrap: anywhere; }
  .meta { font-size: 0.875rem; }
  img { object-fit: cover; }
  .danger { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 1rem; }
</style>
