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
  import Button from '#lib/ui/atoms/Button.svelte';
  import Checkbox from '#lib/ui/atoms/Checkbox.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Radio from '#lib/ui/atoms/Radio.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import Dialog from '#lib/ui/molecules/Dialog.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';

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

  let open = $state(true);

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

<Dialog bind:open title={t('add.title')} {onclose}>
  <div class="add-sheet">
    <p class="edition"><strong>{edition.title}</strong> <span class="muted">{sourceName}</span></p>
    {#if shelvesProblem}
      <ProblemCard problem={shelvesProblem} level={2} />
    {/if}

    {#if estimateLoading}
      <p role="status" class="muted">{t('add.counting')}</p>
    {:else if estimateProblem}
      <ProblemCard problem={estimateProblem} level={2} />
      <div><Button onclick={fetchEstimate}>{t('add.retry')}</Button></div>
    {:else if estimate}
      <p class="summary num" data-testid="add-summary">
        {t('add.summary', { units: formatNumber(estimate.units), bytes: formatNumber(estimate.size_bytes) })}
      </p>
    {/if}

    {#if shelves.length > 0}
      <Field label={t('add.shelf')}>
        {#snippet children(control)}
          <Select
            {...control}
            bind:value={selectedShelf}
            options={shelves.map((shelf) => ({ value: shelf.id, label: shelf.name }))}
          />
        {/snippet}
      </Field>
    {:else if !shelvesProblem}
      <p class="muted">{t('add.unshelved')}</p>
    {/if}

    <fieldset class="modes">
      <legend>{t('add.mode_group')}</legend>
      <Radio name="mode" value="all" bind:group={mode} label={t('add.mode.all')} />
      <Radio name="mode" value="from_number" bind:group={mode} label={t('add.mode.from_number')} />
      <Radio name="mode" value="new_only" bind:group={mode} label={t('add.mode.new_only')} />
    </fieldset>

    {#if mode === 'from_number'}
      <Field label={t('add.from_number')}>
        {#snippet children(control)}
          <Input {...control} type="text" bind:value={fromNumber} width="short" />
        {/snippet}
      </Field>
    {/if}

    {#if estimate && estimate.needs_confirmation}
      <Checkbox bind:checked={confirmed} label={t('add.confirm_size')} />
    {/if}

    {#if spaceRefusal}
      <Alert tone="danger" live="assertive" title={t('add.refusal.estimated', { bytes: formatNumber(spaceRefusal.estimated) })}>
        <p>{t('add.refusal.free', { bytes: formatNumber(spaceRefusal.free) })}</p>
        <p>{t('add.refusal.reserve', { bytes: formatNumber(spaceRefusal.reserve) })}</p>
        {#snippet actions()}
          <Button onclick={() => (trashConfirmOpen = true)}>{t('add.empty_trash')}</Button>
        {/snippet}
      </Alert>
      {#if trashConfirmOpen}
        <div class="trash">
          <Field label={t('add.empty_trash_prompt')}>
            {#snippet children(control)}
              <Input {...control} aria-label={t('add.empty_trash_input')} type="text" mono bind:value={trashWord} />
            {/snippet}
          </Field>
          <div>
            <Button variant="danger" disabled={trashWord !== 'empty-trash' || busy} onclick={emptyTrash}>
              {t('add.empty_trash_now')}
            </Button>
          </div>
          {#if reauthMethods}
            <ReauthPrompt methods={reauthMethods} ondone={emptyTrash} oncancel={() => (reauthMethods = null)} />
          {/if}
        </div>
      {/if}
    {/if}

    {#if actionProblem}
      <ProblemCard problem={actionProblem} level={2} />
    {/if}

    {#if statusMessage}
      <Alert tone="success" live="polite" title={statusMessage} />
    {/if}
  </div>
  {#snippet footer()}
    <Button variant="primary" disabled={!canAdd || busy} loading={busy} onclick={add}>{t('add.submit')}</Button>
  {/snippet}
</Dialog>

<style>
  .add-sheet {
    display: grid;
    gap: var(--space-4);
    min-width: 0;
  }
  .edition {
    margin: 0;
  }
  .summary {
    margin: 0;
    font: 500 var(--step-1) / var(--leading-tight) var(--font-text);
  }
  .modes {
    border: 0;
    padding: 0;
    margin: 0;
    display: grid;
    gap: var(--space-2);
  }
  .modes legend {
    margin-bottom: var(--space-2);
    font-weight: 600;
  }
  .trash {
    display: grid;
    gap: var(--space-3);
    justify-items: start;
  }
</style>
