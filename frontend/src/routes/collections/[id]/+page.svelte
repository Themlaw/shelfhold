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
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Cover from '#lib/ui/atoms/Cover.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import ConfirmDialog from '#lib/ui/molecules/ConfirmDialog.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import ProblemAlert, { blockKind, type BlockKind } from '#lib/ui/molecules/ProblemAlert.svelte';

  const id = $derived(page.params.id ?? '');
  let detail = $state<CollectionDetailOut | null>(null);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);
  let blocked = $state<BlockKind | null>(null);
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
      blocked = blockKind(error);
      detail = null;
    } else {
      problem = null;
      blocked = null;
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

<main class="page">
  {#if !loaded}
    <div class="page-head">
      <div class="page-head__text"><h1 class="page-title">{t('collections.title')}</h1></div>
    </div>
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('collections.loading')}</p>
      <div class="skeleton-rows"><Skeleton /><Skeleton /><Skeleton /><Skeleton short /></div>
    </div>
  {:else if problem || !detail}
    <div class="page-head">
      <div class="page-head__text"><h1 class="page-title">{t('collections.title')}</h1></div>
      <div class="page-head__actions">
        <Button href="/collections" variant="ghost" icon="left">{t('collections.back')}</Button>
      </div>
    </div>
    {#if problem}<ProblemAlert {problem} kind={blocked} onretry={load} />{/if}
  {:else}
    <div class="back">
      <Button href="/collections" variant="ghost" size="sm" icon="left">{t('collections.back')}</Button>
    </div>
    <div class="page-head">
      <div class="page-head__text">
        <h1 class="page-title">{detail.name ?? t('collections.unnamed')}</h1>
        <p class="meta">
          <Badge>{t(`collections.kind.${detail.kind}`)}</Badge>
          <Badge tone={detail.scope === 'shared' ? 'accent' : 'neutral'}>{t(`collections.scope.${detail.scope}`)}</Badge>
          <span class="folio">{t('collections.count', { count: detail.count })}</span>
        </p>
      </div>
      {#if canWrite}
        <div class="page-head__actions">
          <Button variant="danger-ghost" icon="trash" disabled={busy} onclick={() => (confirming = true)}>{t('collections.delete')}</Button>
        </div>
      {/if}
    </div>
    {#if actionProblem}
      <div class="section"><ProblemAlert problem={actionProblem} /></div>
    {/if}
    {#if detail.items.length === 0}
      <EmptyState title={t('collections.items_empty')} headingLevel={2} art={false} />
    {:else}
      <ol class="entries" aria-label={t('collections.items')}>
        {#each detail.items as item (item.id)}
          <li class="item">
            <span class="item__position folio">{item.position}</span>
            <Cover title={item.title} seed={item.work_id} src={item.cover_hash ? thumb(item.cover_hash) : null} size="sm" />
            <span class="item__title">
              {item.title}
              {#if item.number}<span class="item__number folio">{t('collections.number', { number: item.number })}</span>{/if}
            </span>
            {#if canWrite}
              <Button
                variant="ghost"
                size="sm"
                disabled={busy}
                aria-label={t('collections.remove_item', { title: item.title })}
                onclick={() => remove(item.id)}
              >
                {t('collections.remove')}
              </Button>
            {/if}
          </li>
        {/each}
      </ol>
    {/if}
    {#if canWrite}
      <ConfirmDialog
        bind:open={confirming}
        title={t('collections.delete_title')}
        message={t('collections.delete_message')}
        confirmLabel={t('collections.delete_confirm')}
        cancelLabel={t('collections.delete_cancel')}
        onconfirm={destroy}
      />
    {/if}
  {/if}
</main>

<style>
  .skeleton-rows {
    display: grid;
    gap: var(--space-3);
    max-width: 28rem;
  }
  .back {
    margin: 0 0 var(--space-3) calc(-1 * var(--space-3));
  }
  .meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin: var(--space-2) 0 0;
  }
  .item {
    display: grid;
    grid-template-columns: 2.5ch 3rem minmax(0, 1fr) auto;
    gap: var(--space-4);
    align-items: center;
    padding: var(--space-3) 0;
  }
  .item__position {
    text-align: right;
    font-size: var(--step-1);
  }
  .item__title {
    min-width: 0;
    font-weight: 600;
    overflow-wrap: anywhere;
  }
  .item__number {
    display: block;
    font-weight: 400;
  }
</style>
