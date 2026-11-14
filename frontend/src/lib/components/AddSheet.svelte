<script lang="ts">
  // « Ajouter à ma bibliothèque » (EF-UX-006, EF-BIB-030): the count, the size and the shelf of
  // what would be added, the three modes, and the refusal that offers « Vider la corbeille ».
  // The sheet decides nothing: the numbers are the server's, the refusal is the server's, and
  // nothing is written before the one click.
  import { onMount } from 'svelte';
  import { client } from '#lib/api/client.gen.ts';
  import { emptyTrash as emptyTrashApi, follow, shelves as loadShelves, spaceCheck } from '#lib/api/index.ts';
  import type { EstimateOut, ShelfItemOut } from '#lib/api/types.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { t, formatNumber } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, problemOf } from '#lib/triage.ts';

  type Mode = 'all' | 'from_number' | 'new_only';

  let {
    edition,
    sourceName,
    onclose,
    onadded
  }: {
    edition: { source_key: string; remote_id: string; title: string; format: string };
    sourceName: string;
    onclose: () => void;
    onadded: (state: string) => void;
  } = $props();

  let dialogEl: HTMLDivElement | undefined = $state();

  let mode = $state<Mode>('all');
  let fromNumber = $state('');
  let shelves = $state<ShelfItemOut[]>([]);
  let shelvesProblem = $state<ProblemView | null>(null);
  let selectedShelf = $state('');
  let estimate = $state<EstimateOut | null>(null);
  let estimateProblem = $state<ProblemView | null>(null);
  let estimateLoading = $state(false);
  let estimateToken = 0;
  let confirmed = $state(false);
  let busy = $state(false);
  let actionProblem = $state<ProblemView | null>(null);
  let statusMessage = $state<string | null>(null);
  let spaceRefusal = $state<{ estimated: number; free: number; reserve: number } | null>(null);
  let trashConfirmOpen = $state(false);
  let trashWord = $state('');
  let reauthMethods = $state<string[] | null>(null);

  const canAdd = $derived(
    !busy &&
      !estimateLoading &&
      estimate !== null &&
      estimateProblem === null &&
      shelvesProblem === null &&
      (mode !== 'from_number' || fromNumber.trim() !== '') &&
      (!estimate.needs_confirmation || confirmed) &&
      spaceRefusal === null
  );

  onMount(async () => {
    dialogEl?.focus();
    const { data, error } = await loadShelves({ throwOnError: false });
    if (error !== undefined) {
      shelvesProblem = problemView(error);
    } else {
      shelves = data?.items ?? [];
      selectedShelf = shelves[0]?.id ?? '';
    }
  });

  async function fetchEstimate() {
    const token = ++estimateToken;
    estimateLoading = true;
    estimateProblem = null;
    estimate = null;
    actionProblem = null;
    statusMessage = null;
    if (mode === 'from_number' && !fromNumber.trim()) {
      estimateLoading = false;
      return;
    }
    const { data, error } = await client.get({
      url: '/api/v1/sources/{source_key}/series/{remote_id}/estimate',
      path: { source_key: edition.source_key, remote_id: edition.remote_id },
      query: {
        mode,
        ...(mode === 'from_number' ? { from_number: fromNumber.trim() } : {})
      },
      throwOnError: false
    });
    if (token !== estimateToken) return;
    estimateLoading = false;
    if (error !== undefined) {
      estimateProblem = problemView(error);
      return;
    }
    estimate = data as EstimateOut;
  }

  $effect(() => {
    void mode;
    void fromNumber;
    void fetchEstimate();
  });

  const FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  // The sheet is modal: Tab and Shift+Tab go round inside it, never to the page behind.
  function trapFocus(event: KeyboardEvent) {
    if (!dialogEl) return;
    const inside = Array.from(dialogEl.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (inside.length === 0) {
      event.preventDefault();
      dialogEl.focus();
      return;
    }
    const first = inside[0]!;
    const last = inside[inside.length - 1]!;
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === dialogEl)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    } else if (!dialogEl.contains(active)) {
      event.preventDefault();
      first.focus();
    }
  }

  function onKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      event.preventDefault();
      onclose();
    } else if (event.key === 'Tab') {
      trapFocus(event);
    }
  }

  function refusalOf(problem: unknown, fallbackEstimate: number) {
    const figures = problem as { estimated_bytes?: unknown; free_bytes?: unknown; reserve_bytes?: unknown };
    return {
      estimated: Number(figures.estimated_bytes ?? fallbackEstimate),
      free: Number(figures.free_bytes ?? 0),
      reserve: Number(figures.reserve_bytes ?? 0)
    };
  }

  async function add() {
    if (!canAdd || estimate === null) return;
    busy = true;
    actionProblem = null;
    spaceRefusal = null;
    statusMessage = null;
    try {
      const { error: spaceError } = await spaceCheck({
        headers: { 'X-CSRF-Token': csrfToken() },
        body: { estimated_bytes: estimate.size_bytes },
        throwOnError: false
      });
      if (spaceError !== undefined) {
        if (problemOf(spaceError).code === 'library.space_insufficient') {
          spaceRefusal = refusalOf(spaceError, estimate.size_bytes);
        } else {
          actionProblem = problemView(spaceError);
        }
        return;
      }
      const body: Record<string, unknown> = {
        source_key: edition.source_key,
        remote_id: edition.remote_id,
        mode
      };
      if (mode === 'from_number') body.from_number = fromNumber.trim();
      if (selectedShelf) body.shelf_id = selectedShelf;
      if (estimate.needs_confirmation) {
        body.confirm_units = estimate.units;
        body.confirm_bytes = estimate.size_bytes;
      }
      const { data, error: followError } = await follow({
        headers: { 'X-CSRF-Token': csrfToken() },
        body: body as never,
        throwOnError: false
      });
      if (followError !== undefined) {
        if (needsReauth(followError)) {
          reauthMethods = problemOf(followError).reauth_methods ?? ['password'];
        } else if (problemOf(followError).code === 'backfill.confirmation_required') {
          // the series moved since it was counted: the figures are counted again, and confirmed again
          confirmed = false;
          await fetchEstimate();
          statusMessage = t('add.estimate_changed');
        } else {
          actionProblem = problemView(followError);
        }
        return;
      }
      const state = (data as { state: string }).state;
      statusMessage = state === 'in_progress' ? t('add.in_progress') : t('add.added');
      onadded(state);
    } finally {
      busy = false;
    }
  }

  async function emptyTrash() {
    if (trashWord !== 'empty-trash') return;
    busy = true;
    actionProblem = null;
    try {
      const { error: trashError } = await emptyTrashApi({
        headers: { 'X-CSRF-Token': csrfToken() },
        body: { typed: trashWord },
        throwOnError: false
      });
      if (trashError !== undefined) {
        if (needsReauth(trashError)) {
          reauthMethods = problemOf(trashError).reauth_methods ?? ['password'];
        } else {
          actionProblem = problemView(trashError);
        }
        return;
      }
      reauthMethods = null;
      // check space again with the same estimate
      if (estimate !== null) {
        const { error: spaceError } = await spaceCheck({
          headers: { 'X-CSRF-Token': csrfToken() },
          body: { estimated_bytes: estimate.size_bytes },
          throwOnError: false
        });
        if (spaceError !== undefined && problemOf(spaceError).code === 'library.space_insufficient') {
          spaceRefusal = refusalOf(spaceError, estimate.size_bytes);
        } else {
          spaceRefusal = null;
          statusMessage = t('add.trash_emptied');
        }
      } else {
        spaceRefusal = null;
        statusMessage = t('add.trash_emptied');
      }
      trashConfirmOpen = false;
      trashWord = '';
    } finally {
      busy = false;
    }
  }
</script>

<div
  class="add-sheet"
  role="dialog"
  aria-modal="true"
  aria-label={t('add.title')}
  tabindex="-1"
  bind:this={dialogEl}
  onkeydown={onKeydown}
>
  <header>
    <h2>{edition.title}</h2>
    <p class="source">{sourceName}</p>
    <button type="button" class="close" onclick={() => onclose()}>{t('add.close')}</button>
  </header>

  {#if shelvesProblem}
    <ProblemCard problem={shelvesProblem} level={2} />
  {/if}

  {#if estimateLoading}
    <p role="status">{t('add.counting')}</p>
  {:else if estimateProblem}
    <ProblemCard problem={estimateProblem} level={2} />
    <button type="button" onclick={fetchEstimate}>{t('add.retry')}</button>
  {:else if estimate}
    <p class="summary" data-testid="add-summary">
      {t('add.summary', { units: formatNumber(estimate.units), bytes: formatNumber(estimate.size_bytes) })}
    </p>
  {/if}

  {#if shelves.length > 0}
    <label class="shelf">
      {t('add.shelf')}
      <select aria-label={t('add.shelf')} bind:value={selectedShelf}>
        {#each shelves as shelf (shelf.id)}
          <option value={shelf.id}>{shelf.name}</option>
        {/each}
      </select>
    </label>
  {:else if !shelvesProblem}
    <p class="unshelved">{t('add.unshelved')}</p>
  {/if}

  <fieldset class="modes">
    <legend>{t('add.mode_group')}</legend>
    <label><input type="radio" name="mode" value="all" bind:group={mode} /> {t('add.mode.all')}</label>
    <label><input type="radio" name="mode" value="from_number" bind:group={mode} /> {t('add.mode.from_number')}</label>
    <label><input type="radio" name="mode" value="new_only" bind:group={mode} /> {t('add.mode.new_only')}</label>
  </fieldset>

  {#if mode === 'from_number'}
    <label class="number">
      {t('add.from_number')}
      <input type="text" aria-label={t('add.from_number')} bind:value={fromNumber} />
    </label>
  {/if}

  {#if estimate && estimate.needs_confirmation}
    <label class="confirm">
      <input type="checkbox" bind:checked={confirmed} />
      {t('add.confirm_size')}
    </label>
  {/if}

  {#if spaceRefusal}
    <section role="alert" class="refusal">
      <p>{t('add.refusal.estimated', { bytes: formatNumber(spaceRefusal.estimated) })}</p>
      <p>{t('add.refusal.free', { bytes: formatNumber(spaceRefusal.free) })}</p>
      <p>{t('add.refusal.reserve', { bytes: formatNumber(spaceRefusal.reserve) })}</p>
      <button type="button" onclick={() => (trashConfirmOpen = true)}>{t('add.empty_trash')}</button>
      {#if trashConfirmOpen}
        <label>
          {t('add.empty_trash_prompt')}
          <input type="text" aria-label={t('add.empty_trash_input')} bind:value={trashWord} />
        </label>
        <button type="button" disabled={trashWord !== 'empty-trash' || busy} onclick={emptyTrash}>
          {t('add.empty_trash_now')}
        </button>
        {#if reauthMethods}
          <ReauthPrompt methods={reauthMethods} ondone={emptyTrash} oncancel={() => (reauthMethods = null)} />
        {/if}
      {/if}
    </section>
  {/if}

  {#if actionProblem}
    <ProblemCard problem={actionProblem} level={2} />
  {/if}

  {#if statusMessage}
    <p role="status">{statusMessage}</p>
  {/if}

  <footer class="actions">
    <button type="button" disabled={!canAdd || busy} onclick={add}>{t('add.submit')}</button>
  </footer>
</div>

<style>
  .add-sheet {
    display: grid;
    gap: 0.75rem;
    padding: 1rem;
    border: 1px solid var(--color-rule);
    border-radius: var(--radius-cover);
    background: var(--color-surface);
    max-width: 32rem;
  }
  header {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  h2 {
    margin: 0;
    font-size: 1.125rem;
  }
  .source {
    color: var(--color-muted);
    margin: 0;
  }
  .close {
    margin-left: auto;
  }
  .summary {
    margin: 0;
    color: var(--color-muted);
  }
  .modes {
    border: none;
    padding: 0;
    margin: 0;
    display: grid;
    gap: 0.25rem;
  }
  .modes label {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }
  .shelf select,
  .number input {
    min-height: 2.75rem;
  }
  .confirm {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }
  .refusal {
    display: grid;
    gap: 0.5rem;
    border: 1px solid var(--color-rule);
    border-radius: var(--radius-cover);
    padding: 1rem;
  }
  .actions button {
    min-height: 2.75rem;
    min-width: 2.75rem;
    padding: 0 1rem;
    border-radius: 0.5rem;
    border: 1px solid currentColor;
    background: var(--color-accent);
    color: var(--color-on-accent);
    cursor: pointer;
  }
  .actions button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
