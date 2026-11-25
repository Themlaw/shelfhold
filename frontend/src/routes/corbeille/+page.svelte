<script lang="ts">
  // "Corbeille" (EF-BIB-021) and its two ways out for the admin alone: "Empty now" (EF-BIB-030)
  // and, for one Unit, "Erase now" (EF-BIB-052). Each asks for a word typed again, and the
  // server compares it: the button is only a convenience. A stale password is asked for in place.
  // The list shows no path and no title: an identifier, a size and a term.
  import { onMount } from 'svelte';
  import { emptyTrash, purgeUnit, trashList } from '#lib/api/index.ts';
  import type { TrashEntryOut } from '#lib/api/types.gen.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { Guard } from '#lib/guard.svelte.ts';
  import { formatDate, formatNumber, t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import ConfirmDialog from '#lib/ui/molecules/ConfirmDialog.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import type { Column } from '#lib/ui/molecules/TableRow.svelte';
  import DataTable from '#lib/ui/organisms/DataTable.svelte';

  /** The word the server expects (`EMPTY_TRASH_PHRASE`); it is only shown, the server compares. */
  const EMPTY_WORD = 'empty-trash';

  const guard = new Guard();
  let entries = $state<TrashEntryOut[]>([]);
  let totalBytes = $state(0);
  let loaded = $state(false);
  let message = $state('');
  let emptyOpen = $state(false);
  let purgeOpen = $state(false);
  let purging = $state<string | null>(null);

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  function size(bytes: number): string {
    const mib = bytes / (1024 * 1024);
    return mib >= 1024
      ? `${formatNumber(mib / 1024, { maximumFractionDigits: 1 })} Gio`
      : `${formatNumber(mib, { maximumFractionDigits: 1 })} Mio`;
  }

  async function load(): Promise<void> {
    await guard.run(async () => {
      const { data } = await trashList({ throwOnError: true });
      entries = data.entries;
      totalBytes = data.total_bytes;
      loaded = true;
    });
  }

  onMount(load);

  async function emptyNow(typedWord: string | undefined): Promise<void> {
    const typed = (typedWord ?? '').trim();
    if (typed !== EMPTY_WORD) return;
    await guard.run(async () => {
      const { data } = await emptyTrash({ headers: headers(), body: { typed }, throwOnError: true });
      const report = data as { removed?: number; held_irreplaceable?: number };
      message = t('trash.emptied', {
        removed: report.removed ?? 0,
        held: report.held_irreplaceable ?? 0
      });
      await load();
    });
  }

  function startPurge(unit: string): void {
    purging = unit;
    purgeOpen = true;
  }

  async function purgeNow(typedUnit: string | undefined, unit: string): Promise<void> {
    const typed = (typedUnit ?? '').trim();
    if (typed !== unit) return;
    await guard.run(async () => {
      await purgeUnit({
        headers: headers(),
        path: { unit_id: unit },
        body: { typed },
        throwOnError: true
      });
      message = t('trash.purged');
      purging = null;
      await load();
    });
  }

  const columns: readonly Column[] = [
    { key: 'unit', label: t('trash.col.unit'), primary: true },
    { key: 'files', label: t('trash.col.files'), num: true },
    { key: 'bytes', label: t('trash.col.size'), num: true },
    { key: 'deleted_at', label: t('trash.col.deleted') },
    { key: 'purge_after', label: t('trash.col.until') },
    { key: 'actions', label: t('trash.col.actions'), actions: true, hideLabel: true }
  ];

  const purgeEntry = $derived(entries.find((entry) => entry.unit_id === purging) ?? null);
  const purgeMessage = $derived(
    purgeEntry?.irreplaceable
      ? `${t('trash.purge_warning')} ${t('trash.purge_warning_irreplaceable')}`
      : t('trash.purge_warning')
  );

  const MESSAGES = {
    signed_out: 'trash.signed_out',
    forbidden: 'trash.forbidden',
    failed: 'trash.error'
  } as const;
</script>

<svelte:head>
  <title>{t('trash.title')}</title>
</svelte:head>

<main class="page">
  <div class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{t('trash.title')}</h1>
      <p class="lede">{t('trash.intro')}</p>
    </div>
  </div>

  {#if guard.state === 'signed_out' || guard.state === 'forbidden'}
    <Alert tone="danger" live="assertive" title={t(MESSAGES[guard.state])} />
  {:else}
    <div class="stack">
      {#if guard.state === 'failed'}
        <Alert tone="danger" live="assertive" title={t(MESSAGES.failed)} />
      {/if}
      {#if guard.reauthMethods}
        <ReauthPrompt
          methods={guard.reauthMethods}
          ondone={() => guard.afterReauth()}
          oncancel={() => guard.cancelReauth()}
        />
      {/if}
      {#if message}
        <Alert tone="success" live="polite" title={message} />
      {/if}

      {#if !loaded && guard.state === 'ok'}
        <div aria-busy="true">
          <p class="visually-hidden" role="status">{t('trash.loading')}</p>
          <div class="loading">
            {#each { length: 4 }, index (index)}
              <Skeleton />
            {/each}
          </div>
        </div>
      {:else if loaded && entries.length === 0}
        <EmptyState title={t('trash.none')} />
      {:else if entries.length > 0}
        <p class="folio">{t('trash.total', { size: size(totalBytes) })}</p>
        <DataTable
          caption={t('trash.caption')}
          captionHidden
          {columns}
          rows={entries}
          key={(entry) => entry.deletion_id + entry.unit_id}
          tone={(entry) => (entry.irreplaceable ? 'danger' : undefined)}
          compact
        >
          {#snippet cell(column, entry)}
            {#if column.key === 'unit'}
              <span class="unit">
                <code class="long">{entry.unit_id}</code>
                {#if entry.irreplaceable}
                  <Badge tone="danger">{t('trash.irreplaceable')}</Badge>
                {/if}
              </span>
            {:else if column.key === 'files'}
              {formatNumber(entry.files)}
            {:else if column.key === 'bytes'}
              {size(entry.bytes)}
            {:else if column.key === 'deleted_at'}
              {formatDate(entry.deleted_at)}
            {:else if column.key === 'purge_after'}
              {formatDate(entry.purge_after)}
            {:else}
              <Button variant="danger-ghost" size="sm" onclick={() => startPurge(entry.unit_id)}>
                {t('trash.purge')}
              </Button>
            {/if}
          {/snippet}
        </DataTable>

        <section aria-labelledby="empty-title" class="panel empty">
          <h2 id="empty-title">{t('trash.empty_title')}</h2>
          <p>{t('trash.empty_message')}</p>
          <div class="btn-row">
            <Button variant="danger" onclick={() => (emptyOpen = true)}>
              {t('trash.empty_open')}
            </Button>
          </div>
        </section>
      {/if}
    </div>

    <ConfirmDialog
      bind:open={purgeOpen}
      title={t('trash.purge_title', { unit: purging ?? '' })}
      message={purgeMessage}
      confirmLabel={t('trash.purge_confirm')}
      cancelLabel={t('trash.purge_cancel')}
      phrase={purging ?? undefined}
      onconfirm={(typed) => purgeNow(typed, purging ?? '')}
      oncancel={() => (purging = null)}
    />
    <ConfirmDialog
      bind:open={emptyOpen}
      title={t('trash.empty_title')}
      message={t('trash.empty_hint', { word: EMPTY_WORD })}
      confirmLabel={t('trash.empty_run')}
      phrase={EMPTY_WORD}
      onconfirm={emptyNow}
    />
  {/if}
</main>

<style>
  .loading {
    display: grid;
    gap: var(--space-4);
  }
  .unit {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2) var(--space-3);
    min-width: 0;
  }
  .long {
    overflow-wrap: anywhere;
  }
  .empty {
    display: grid;
    gap: var(--space-3);
    justify-items: start;
  }
  .empty > h2,
  .empty > p {
    margin: 0;
  }
</style>
