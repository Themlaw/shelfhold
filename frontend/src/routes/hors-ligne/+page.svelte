<script lang="ts">
  // The copies kept on this device (M5 L16b, EF-LEC-012): the signed-in profile's, and nobody
  // else's. It opens with no network (the shell is kept by the worker, the list is read from the
  // device). M6 L20: the list follows the changes — a copy kept or evicted, in this tab or in
  // another, is read again without a reload. Titles are inserted as text.
  import { onMount } from 'svelte';
  import { formatBytes } from '#lib/bytes.ts';
  import { formatDate, t } from '#lib/i18n/index.ts';
  import { offlineStore, onOfflineChanged } from '#lib/offline/runtime.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import type { CopyMeta } from '#lib/offline/store.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import { copySize } from './copy-size.ts';

  let copies = $state<CopyMeta[]>([]);
  let profile = $state<string | null>(null);
  let loaded = $state(false);
  let failed = $state(false);
  let sizes = $state<Record<string, number | null>>({});
  let removing = $state<string | null>(null);

  const keyOf = (copy: CopyMeta) => copy.carrierType + copy.carrierId;

  async function load(): Promise<void> {
    try {
      const store = offlineStore();
      profile = await store.activeProfile();
      const token = csrfToken();
      // another session than the one the copies were kept under (another profile signed in elsewhere): nothing is listed
      const foreign = token !== '' && (await store.sessionMatches(token)) === false;
      copies = profile && !foreign ? await store.copies(profile) : [];
      failed = false;
    } catch {
      copies = [];
      failed = true;
    }
    loaded = true;
    void measure();
  }

  /** The size of each copy, read from the cache of the profile once the list is shown. */
  async function measure(): Promise<void> {
    const owner = profile;
    if (!owner) return;
    const known = await Promise.all(
      copies.map(async (copy) => [keyOf(copy), await copySize(globalThis.caches, owner, copy)] as const)
    );
    sizes = Object.fromEntries(known);
  }

  async function remove(copy: CopyMeta): Promise<void> {
    removing = keyOf(copy);
    try {
      if (profile) await offlineStore().evictCarrier(profile, copy.carrierType, copy.carrierId);
      await load();
    } finally {
      removing = null;
    }
  }

  onMount(() => {
    void load();
    // what the profile's copies became, here or in another tab: the list is read again
    return onOfflineChanged(() => void load());
  });
</script>

<svelte:head><title>{t('offline.library')}</title></svelte:head>

<main class="page page--read">
  <header class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{t('offline.library')}</h1>
      <p class="lede">{t('offline.lead')}</p>
    </div>
  </header>

  {#if !loaded}
    <div class="loading" aria-busy="true">
      <p class="visually-hidden" role="status">{t('offline.loading')}</p>
      {#each [0, 1, 2] as row (row)}
        <div class="skeleton-card"><Skeleton /><Skeleton short /></div>
      {/each}
    </div>
  {:else if failed}
    <Alert tone="danger" title={t('offline.load_failed_title')} live="assertive" headingLevel={2}>
      <p>{t('offline.load_failed')}</p>
      {#snippet actions()}
        <Button icon="refresh" onclick={() => void load()}>{t('error.retry')}</Button>
      {/snippet}
    </Alert>
  {:else if copies.length === 0}
    <EmptyState title={t('offline.empty_title')}>
      <p>{t('offline.empty')}</p>
      <p class="hint">{t('offline.empty_hint')}</p>
      {#snippet actions()}
        <Button href="/bibliotheque" variant="primary" icon="books">{t('offline.browse')}</Button>
      {/snippet}
    </EmptyState>
  {:else}
    <p class="folio count">{t('offline.copy_count', { count: copies.length })}</p>
    <ul class="entries">
      {#each copies as copy (keyOf(copy))}
        {@const size = sizes[keyOf(copy)]}
        <li class="copy">
          <div class="copy__text">
            <span class="copy__title">{copy.title}</span>
            <span class="copy__meta">
              <small>{t('offline.kept_at', { date: formatDate(new Date(copy.keptAt)) })}</small>
              <small class="num">{size != null ? t('offline.size', { size: formatBytes(size) }) : t('offline.size_unknown')}</small>
              <Badge icon="offline">{t('offline.kept')}</Badge>
            </span>
          </div>
          <div class="copy__actions">
            <!-- the reader is a page of its own, under its own policy (ADR-0055): a full navigation -->
            <Button href={`/lire/${copy.carrierType}/${copy.carrierId}`} data-sveltekit-reload variant="primary" icon="read">{t('offline.read')}</Button>
            <Button variant="danger-ghost" icon="trash" loading={removing === keyOf(copy)} onclick={() => remove(copy)}>{t('offline.remove')}</Button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</main>

<style>
  main {
    padding: var(--space-5) var(--space-4);
  }
  .count {
    margin: 0 0 var(--space-3);
  }
  .copy {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3) var(--space-5);
    padding-block: var(--space-4);
  }
  .copy__text {
    display: grid;
    flex: 1 1 16rem;
    gap: var(--space-1);
    min-width: 0;
  }
  .copy__title {
    font: 600 var(--step-05) / var(--leading-ui) var(--font-text);
    overflow-wrap: anywhere;
  }
  .copy__meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1) var(--space-4);
    color: var(--color-muted);
  }
  .copy__actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  .skeleton-card {
    padding-block: var(--space-4);
    border-bottom: var(--rule-hair) solid var(--color-rule);
  }
  /* a phone: each copy is a card, its actions across the full width */
  @media (max-width: 40rem) {
    .copy {
      padding: var(--space-4);
      margin-block: var(--space-3);
      background: var(--color-surface);
      border: var(--rule-hair) solid var(--color-rule);
      border-radius: var(--radius-md);
    }
    .entries {
      border-top: 0;
    }
    .entries > li {
      border-bottom: var(--rule-hair) solid var(--color-rule);
    }
    .copy__actions {
      flex: 1 1 100%;
    }
    .copy__actions > :global(*) {
      flex: 1 1 auto;
      justify-content: center;
    }
  }
</style>
