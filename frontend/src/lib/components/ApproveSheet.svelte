<script lang="ts">
  // "À approuver" (EF-USR-010): one gesture to approve, with a mandatory age selector for a
  // child's request, the size summary, and a confirmation beyond 100 units or 5 GiB.
  import { onMount } from 'svelte';
  import { approve, approval as fetchApproval } from '#lib/api/index.ts';
  import type { Age } from '#lib/triage.ts';
  import type { ApprovalOut } from '#lib/api/types.gen.ts';
  import AgePicker from '#lib/components/AgePicker.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';

  // Flexible props: the sheet may be driven by a queue item, a request id, or a preview.
  let props: Record<string, any> = $props();

  const id = $derived(
    props.requestId ?? props.request_id ?? props.id ??
    props.item?.id ?? props.request?.id ?? props.queueItem?.id ?? props.row?.id ?? null
  );
  const title = $derived(
    props.title ?? props.item?.title ?? props.request?.title ?? props.queueItem?.title ?? props.row?.title ?? null
  );
  const needsAge = $derived(
    Boolean(
      props.needsAge ?? props.needs_age ??
      props.item?.needs_age ?? props.request?.needs_age ?? props.queueItem?.needs_age ?? props.row?.needs_age ??
      preview?.needs_age
    )
  );

  let preview = $state<ApprovalOut | null>(props.preview ?? props.approval ?? props.data ?? null);
  let loading = $state(false);
  let age = $state<Age | null>(null);
  let x = $state(false);
  let shelfId = $state<string | null>(null);
  let confirmSize = $state(false);
  let confirmText = $state('');
  let message = $state('');
  let failed = $state(false);

  async function loadPreview(): Promise<void> {
    if (!id || preview) return;
    loading = true;
    try {
      const { data, error } = await fetchApproval({ path: { request_id: id } });
      if (error === undefined) preview = data ?? null;
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    void loadPreview();
    if (preview?.shelf_id) shelfId = preview.shelf_id;
    if (preview?.decided_age != null) age = `${preview.decided_age}+` as Age;
    if (preview?.decided_x) x = true;
  });

  const canApprove = $derived(
    (!needsAge || age !== null) &&
    (!(preview?.confirmation_required) || confirmSize)
  );

  async function doApprove(): Promise<void> {
    const body = {
      age: age ? Number.parseInt(age, 10) || null : null,
      x,
      shelf_id: shelfId ?? preview?.shelf_id ?? null,
      confirm_size: confirmSize
    };
    // Age is stored as "13+" strings; convert back to the number the API expects.
    if (age && age.endsWith('+')) body.age = Number.parseInt(age, 10);
    if (age === 'X') {
      body.age = null;
      body.x = true;
    }
    if (props.onApprove ?? props.onapprove ?? props.approve) {
      (props.onApprove ?? props.onapprove ?? props.approve)(body);
      return;
    }
    if (!id) return;
    try {
      await approve({
        headers: { 'X-CSRF-Token': csrfToken() },
        path: { request_id: id },
        body
      });
      props.onApproved?.();
    } catch (error) {
      failed = true;
      message = t('toRate.error');
    }
  }
</script>

<section class="approve-sheet" aria-label={t('approve.title')}>
  <h2>{t('approve.title')}</h2>
  {#if title}
    <p class="title">{title}</p>
  {/if}

  {#if loading}
    <p>{t('home.loading')}</p>
  {:else}
    {#if preview}
      <p class="summary">
        {t('approve.size_summary', { units: preview.units, bytes: preview.size_bytes ?? 0 })}
        {#if preview.units_minimum} ({t('approve.more_to_come')}){/if}
      </p>
    {/if}

    {#if needsAge}
      <AgePicker name={`age-${id ?? 'request'}`} legend={t('approve.age_for_child')} bind:value={age} />
      {#if !age}
        <p class="hint">{t('approve.choose_age')}</p>
      {/if}
    {/if}

    {#if preview?.confirmation_required}
      <label class="confirm">
        <input type="checkbox" bind:checked={confirmSize} />
        {t('approve.confirm_size')}
      </label>
    {/if}

    {#if message}
      <p role={failed ? 'alert' : 'status'} class:err={failed}>{message}</p>
    {/if}

    <div class="actions">
      <button type="button" disabled={!canApprove} onclick={doApprove}>
        {t('approve.approve')}
      </button>
    </div>
  {/if}
</section>

<style>
  .approve-sheet {
    display: grid;
    gap: 0.75rem;
    padding: 1rem;
    border: 1px solid var(--color-rule);
    border-radius: var(--radius-cover);
    background: var(--color-surface);
  }
  h2 {
    margin: 0;
    font-size: 1.125rem;
  }
  .title {
    font-weight: 600;
    margin: 0;
  }
  .summary {
    margin: 0;
    color: var(--color-muted);
  }
  .hint {
    margin: 0;
    color: var(--color-err);
  }
  .confirm {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }
  .actions button {
    min-height: 2.75rem;
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
  .err {
    color: var(--color-err);
  }
</style>
