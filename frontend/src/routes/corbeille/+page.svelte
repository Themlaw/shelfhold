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

  /** The word the server expects (`EMPTY_TRASH_PHRASE`); it is only shown, the server compares. */
  const EMPTY_WORD = 'empty-trash';

  const guard = new Guard();
  let entries = $state<TrashEntryOut[]>([]);
  let totalBytes = $state(0);
  let loaded = $state(false);
  let message = $state('');
  let emptyTyped = $state('');
  let purging = $state<string | null>(null);
  let purgeTyped = $state('');

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

  async function emptyNow(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const typed = emptyTyped.trim();
    if (typed !== EMPTY_WORD) return;
    await guard.run(async () => {
      const { data } = await emptyTrash({ headers: headers(), body: { typed }, throwOnError: true });
      const report = data as { removed?: number; held_irreplaceable?: number };
      message = t('trash.emptied', {
        removed: report.removed ?? 0,
        held: report.held_irreplaceable ?? 0
      });
      emptyTyped = '';
      await load();
    });
  }

  function startPurge(unit: string): void {
    purging = unit;
    purgeTyped = '';
  }

  async function purgeNow(event: SubmitEvent, unit: string): Promise<void> {
    event.preventDefault();
    const typed = purgeTyped.trim();
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
      purgeTyped = '';
      await load();
    });
  }

  const MESSAGES = {
    signed_out: 'trash.signed_out',
    forbidden: 'trash.forbidden',
    failed: 'trash.error'
  } as const;
</script>

<svelte:head>
  <title>{t('trash.title')}</title>
</svelte:head>

<main>
  <h1>{t('trash.title')}</h1>

  {#if guard.state === 'signed_out' || guard.state === 'forbidden'}
    <p role="alert" class="err">{t(MESSAGES[guard.state])}</p>
  {:else}
    {#if guard.state === 'failed'}
      <p role="alert" class="err">{t(MESSAGES.failed)}</p>
    {/if}
    {#if guard.reauthMethods}
      <ReauthPrompt
        methods={guard.reauthMethods}
        ondone={() => guard.afterReauth()}
        oncancel={() => guard.cancelReauth()}
      />
    {/if}
    {#if message}
      <p role="status">{message}</p>
    {/if}

    <p>{t('trash.intro')}</p>

    {#if loaded && entries.length === 0}
      <p>{t('trash.none')}</p>
    {:else if entries.length > 0}
      <p class="muted">{t('trash.total', { size: size(totalBytes) })}</p>
      <ul class="entries">
        {#each entries as entry (entry.deletion_id + entry.unit_id)}
          <li aria-label={t('trash.entry', { unit: entry.unit_id })}>
            <div class="head">
              <code>{entry.unit_id}</code>
              {#if entry.irreplaceable}
                <strong class="rare">{t('trash.irreplaceable')}</strong>
              {/if}
            </div>
            <p class="muted">
              {t('trash.line', {
                files: entry.files,
                size: size(entry.bytes),
                deleted: formatDate(entry.deleted_at),
                until: formatDate(entry.purge_after)
              })}
            </p>
            {#if purging === entry.unit_id}
              <form onsubmit={(event) => purgeNow(event, entry.unit_id)}>
                <p class="err">{t('trash.purge_warning')}</p>
                {#if entry.irreplaceable}
                  <p class="err"><strong>{t('trash.purge_warning_irreplaceable')}</strong></p>
                {/if}
                <label for={`purge-${entry.unit_id}`}>{t('trash.purge_label', { unit: entry.unit_id })}</label>
                <input
                  id={`purge-${entry.unit_id}`}
                  bind:value={purgeTyped}
                  autocomplete="off"
                  spellcheck="false"
                />
                <div class="row">
                  <button type="submit" disabled={purgeTyped.trim() !== entry.unit_id}>
                    {t('trash.purge_confirm')}
                  </button>
                  <button type="button" class="plain" onclick={() => (purging = null)}>
                    {t('trash.purge_cancel')}
                  </button>
                </div>
              </form>
            {:else}
              <button type="button" class="plain" onclick={() => startPurge(entry.unit_id)}>
                {t('trash.purge')}
              </button>
            {/if}
          </li>
        {/each}
      </ul>

      <section aria-labelledby="empty-title" class="empty">
        <h2 id="empty-title">{t('trash.empty_title')}</h2>
        <form onsubmit={emptyNow}>
          <p>{t('trash.empty_hint', { word: EMPTY_WORD })}</p>
          <label for="empty-word">{t('trash.empty_word')}</label>
          <input id="empty-word" bind:value={emptyTyped} autocomplete="off" spellcheck="false" />
          <button type="submit" disabled={emptyTyped.trim() !== EMPTY_WORD}>
            {t('trash.empty_run')}
          </button>
        </form>
      </section>
    {/if}
  {/if}
</main>

<style>
  main {
    max-width: 42rem;
    margin: 0 auto;
    padding: 3rem 1rem;
  }
  h1 {
    font-family: var(--font-display);
    font-weight: 400;
    font-style: italic;
    letter-spacing: -0.02em;
  }
  .entries {
    list-style: none;
    margin: 1rem 0;
    padding: 0;
    display: grid;
    gap: 1rem;
  }
  .entries li {
    display: grid;
    gap: 0.5rem;
    padding: 1rem;
    border: 1px solid var(--color-rule);
    border-radius: var(--radius-cover);
    background: var(--color-surface);
  }
  .head,
  .row {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 0.5rem 1rem;
    overflow-wrap: anywhere;
  }
  .row {
    justify-content: flex-start;
  }
  form,
  .empty {
    display: grid;
    gap: 0.5rem;
  }
  .empty {
    margin-top: 1.5rem;
  }
  .rare {
    color: var(--color-err);
  }
  .muted {
    color: var(--color-muted);
  }
  .err {
    color: var(--color-err);
  }
  button,
  input {
    min-height: 2.75rem;
  }
</style>
