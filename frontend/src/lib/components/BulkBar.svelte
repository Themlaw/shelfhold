<script lang="ts">
  // Bulk rating bar for the "À classer" screen (EF-BIB-023): explicit selection, preview then one
  // job. Nothing is rated until the preview is applied; the server stays the gate.
  import { bulkRateApply, bulkRatePreview, jobStatus } from '#lib/api/index.ts';
  import type { ToRateItem, BulkRatePreviewOut, JobStatusOut } from '#lib/api/types.gen.ts';
  import AgePicker from '#lib/components/AgePicker.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { untrack } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';
  import { csrfToken } from '#lib/passkeys.ts';
  import { ageLabel, isForbidden, needsReauth, needsSession, problemOf, type Age } from '#lib/triage.ts';

  let {
    items = [],
    stateFilter = '',
    textFilter = '',
    onchanged
  }: {
    items?: ToRateItem[];
    stateFilter?: string;
    textFilter?: string;
    onchanged?: () => void | Promise<void>;
  } = $props();

  const selected = new SvelteSet<string>();
  let age = $state<Age | null>(null);
  let filterMode = $state(false);
  let preview = $state<BulkRatePreviewOut | null>(null);
  let message = $state('');
  let failed = $state(false);
  let busy = $state(false);
  let reauthMethods = $state<string[] | null>(null);
  let retry = $state<(() => Promise<void>) | null>(null);

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  function say(text: string, bad = false): void {
    message = text;
    failed = bad;
  }

  async function guarded(action: () => Promise<void>): Promise<void> {
    try {
      await action();
    } catch (error) {
      if (needsReauth(error)) {
        retry = action;
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else if (needsSession(error)) {
        say(t('toRate.signed_out'), true);
      } else if (isForbidden(error)) {
        say(t('toRate.forbidden'), true);
      } else {
        const problem = problemOf(error);
        const status = problem.status;
        const code = problem.code;
        if (status === 404) {
          say(t('toRate.bulk.not_found'), true);
        } else if (status === 409 && code === 'ops.not_waiting') {
          say(t('toRate.bulk.already_done'), true);
        } else if (status === 409 && code === 'ops.preview_stale') {
          say(t('toRate.bulk.preview_stale'), true);
        } else if (status === 422) {
          say(t('toRate.bulk.invalid'), true);
        } else {
          say(t('toRate.error'), true);
        }
      }
    }
  }

  function tickable(item: ToRateItem): boolean {
    // a Work that is already rated is settled by its own rating, never by a bulk order
    return item.state !== 'suggestion_above' && !item.rated;
  }

  function toggle(id: string): void {
    if (selected.has(id)) selected.delete(id);
    else selected.add(id);
  }

  function selectAllDisplayed(): void {
    for (const item of items) {
      if (tickable(item)) selected.add(item.work_id);
    }
  }

  function deselectAll(): void {
    selected.clear();
  }

  // A new filter is a new question: what was ticked under the old one is not carried over.
  $effect(() => {
    void stateFilter;
    void textFilter;
    untrack(() => selected.clear());
  });

  const canPreview = $derived(age !== null && (filterMode || selected.size > 0));

  async function doPreview(): Promise<void> {
    if (!age || busy) return;
    const chosenAge = age;
    await guarded(async () => {
      const body: Record<string, unknown> = { age: chosenAge };
      if (filterMode) {
        body.filter = { state: stateFilter || null, text: textFilter.trim() || null };
      } else {
        body.work_ids = [...selected];
      }
      const { data } = await bulkRatePreview({
        headers: headers(),
        body: body as never,
        throwOnError: true
      });
      preview = data as BulkRatePreviewOut;
      message = '';
      failed = false;
    });
  }

  async function applyPreview(): Promise<void> {
    if (!preview || busy) return;
    const order = preview;
    const count = preview.items.length;
    busy = true;
    try {
      await guarded(async () => {
        const { data } = await bulkRateApply({
          headers: headers(),
          path: { order_id: order.order_id },
          body: { confirm: order.token },
          throwOnError: true
        });
        const accepted = data as { job_id: string; status_url: string };
        preview = null;
        say(t('toRate.bulk.pending'), false);
        await pollJob(accepted.job_id, count);
      });
    } finally {
      busy = false;
    }
  }

  async function pollJob(jobId: string, count: number): Promise<void> {
    const max = 30;
    for (let i = 0; i < max; i++) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      try {
        const { data } = await jobStatus({
          path: { job_id: jobId },
          throwOnError: true
        });
        const status = (data as JobStatusOut).status;
        const code = (data as JobStatusOut).error_code;
        if (status === 'succeeded') {
          say(t('toRate.bulk.done', { count }), false);
          selected.clear();
          try {
            await onchanged?.();
          } catch {
            // ignore
          }
          return;
        }
        if (status === 'failed' || status === 'dead_letter') {
          if (code === 'bulk.stale') say(t('toRate.bulk.stale'), true);
          else if (code === 'bulk.refused') say(t('toRate.bulk.refused'), true);
          else if (code === 'bulk.unknown') say(t('toRate.bulk.unknown'), true);
          else say(t('toRate.bulk.unknown'), true);
          return;
        }
      } catch (error) {
        if (needsReauth(error)) {
          retry = () => pollJob(jobId, count);
          reauthMethods = problemOf(error).reauth_methods ?? ['password'];
        } else if (needsSession(error)) {
          say(t('toRate.signed_out'), true);
          return;
        } else if (isForbidden(error)) {
          say(t('toRate.forbidden'), true);
          return;
        } else {
          say(t('toRate.error'), true);
          return;
        }
      }
    }
    say(t('toRate.bulk.poll_timeout'), false); // the job goes on: the screen stops looking
  }

  function cancelPreview(): void {
    preview = null;
    message = '';
    failed = false;
  }

  async function afterReauth(): Promise<void> {
    const again = retry;
    retry = null;
    reauthMethods = null;
    if (again) await guarded(again);
  }

  function cancelReauth(): void {
    retry = null;
    reauthMethods = null;
  }
</script>

<section class="bulk" aria-labelledby="bulk-title">
  <h2 id="bulk-title">{t('toRate.bulk.title')}</h2>

  <ul class="bulk-works">
    {#each items as item (item.work_id)}
      <li>
        {#if tickable(item)}
          <label>
            <input
              type="checkbox"
              checked={selected.has(item.work_id)}
              onchange={() => toggle(item.work_id)}
              aria-label={t('toRate.bulk.select_one', { title: item.title })}
            />
            {item.title}
          </label>
        {:else}
          <span>
            {item.title}
            {#if item.rated && item.age !== null && item.age !== undefined}
              <span class="muted">{t('toRate.bulk.already_rated', { age: item.age_x ? 'X' : `${item.age}+` })}</span>
            {/if}
          </span>
        {/if}
      </li>
    {/each}
  </ul>

  <div class="controls">
    <button type="button" onclick={selectAllDisplayed}>{t('toRate.bulk.select_all')}</button>
    <button type="button" onclick={deselectAll}>{t('toRate.bulk.deselect_all')}</button>
    <p role="status" class="counter">{t('toRate.bulk.selected_count', { count: selected.size })}</p>
    <label class="filter-mode">
      <input type="checkbox" bind:checked={filterMode} />
      {t('toRate.bulk.filter_mode')}
    </label>
    <AgePicker name="age-bulk" legend={t('toRate.bulk.age')} bind:value={age} />
    <button type="button" disabled={!canPreview || busy} onclick={doPreview}>
      {t('toRate.bulk.preview')}
    </button>
  </div>

  {#if preview}
    <div class="preview">
      <p>{t('toRate.bulk.preview_summary', { count: preview.items.length, age: t(ageLabel(preview.age as Age)) })}</p>
      <ul class="preview-titles">
        {#each preview.items as item (item.work_id)}
          <li>{item.title}</li>
        {/each}
      </ul>
      {#if preview.skipped > 0}
        <p>{t('toRate.bulk.skipped', { count: preview.skipped })}</p>
      {/if}
      <p>{t('toRate.bulk.expires_at', { value: preview.expires_at })}</p>
      <button type="button" disabled={busy} onclick={applyPreview}>{t('toRate.bulk.apply')}</button>
      <button type="button" disabled={busy} onclick={cancelPreview}>{t('toRate.bulk.cancel')}</button>
    </div>
  {/if}

  {#if message}
    <p role={failed ? 'alert' : 'status'} class:err={failed}>{message}</p>
  {/if}

  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
  {/if}
</section>

<style>
  .bulk {
    display: grid;
    gap: 0.75rem;
    margin-top: 1.5rem;
    padding: 1rem;
    border: 1px solid var(--color-rule);
    border-radius: var(--radius-cover);
    background: var(--color-surface);
  }
  .bulk-works {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 0.25rem;
  }
  .counter {
    margin: 0;
  }
  .controls {
    display: grid;
    gap: 0.5rem;
  }
  .preview {
    display: grid;
    gap: 0.5rem;
  }
  .preview-titles {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .err {
    color: var(--color-err);
  }
  button {
    min-height: 2.75rem;
  }
</style>
