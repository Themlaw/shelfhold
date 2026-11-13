<script lang="ts">
  // "À approuver" (EF-USR-010): one gesture to approve, with the age selector the server asks for
  // a child's request, the size summary and a confirmation beyond 100 Units or 5 GiB. The sheet
  // decides nothing: the server refuses an approval with no age, with no confirmation or from a
  // role that has no right to it, and says why; the button only follows what the preview said.
  import { onMount } from 'svelte';
  import { approve, approval as fetchApproval } from '#lib/api/index.ts';
  import type { ApprovalOut, QueueItemOut } from '#lib/api/types.gen.ts';
  import AgePicker from '#lib/components/AgePicker.svelte';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { formatNumber, t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { AGES, needsReauth, problemOf, type Age } from '#lib/triage.ts';

  let {
    item,
    onapproved
  }: { item: QueueItemOut; onapproved: () => void | Promise<void> } = $props();

  let preview = $state<ApprovalOut | null>(null);
  let problem = $state<ProblemView | null>(null);
  let age = $state<Age | null>(null);
  let confirmed = $state(false);
  let busy = $state(false);
  let reauthMethods = $state<string[] | null>(null);

  /** The age already decided for this request, as the selector writes it. */
  function decided(view: ApprovalOut): Age | null {
    if (view.decided_x) return 'X';
    const candidate = `${view.decided_age}+`;
    return view.decided_age !== null && (AGES as readonly string[]).includes(candidate)
      ? (candidate as Age)
      : null;
  }

  onMount(async () => {
    const { data, error } = await fetchApproval({ path: { request_id: item.id } });
    if (error !== undefined || data === undefined) {
      problem = problemView(error);
      return;
    }
    preview = data;
    age = decided(data);
  });

  const needsAge = $derived(preview?.needs_age ?? item.needs_age);
  const canApprove = $derived(
    !busy && (!needsAge || age !== null) && (!preview?.confirmation_required || confirmed)
  );

  /** The way the server reads a tier: X is the top tier with the X flag. */
  function rating(chosen: Age | null): { age: number | null; x: boolean } {
    if (chosen === null) return { age: null, x: false };
    if (chosen === 'X') return { age: 18, x: true };
    return { age: Number.parseInt(chosen, 10), x: false };
  }

  async function send(): Promise<void> {
    if (!canApprove) return;
    busy = true;
    problem = null;
    try {
      const { error } = await approve({
        headers: { 'X-CSRF-Token': csrfToken() },
        path: { request_id: item.id },
        body: { ...rating(age), shelf_id: null, confirm_size: confirmed }
      });
      if (error === undefined) {
        reauthMethods = null;
        await onapproved();
      } else if (needsReauth(error)) {
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else {
        problem = problemView(error);
      }
    } finally {
      busy = false;
    }
  }
</script>

<section class="approve-sheet" aria-label={t('approve.sheet')}>
  <p class="title">{item.title ?? t('approve.masked')}</p>

  {#if preview}
    <p class="summary">
      {t('approve.size_summary', {
        volumes: preview.units,
        bytes: formatNumber(preview.size_bytes ?? 0)
      })}
      {#if preview.units_minimum}({t('approve.more_to_come')}){/if}
    </p>

    {#if needsAge}
      <AgePicker name={`age-${item.id}`} legend={t('approve.age_for_child')} bind:value={age} />
      {#if age === null}
        <p class="hint">{t('approve.choose_age')}</p>
      {/if}
    {/if}

    {#if preview.confirmation_required}
      <label class="confirm">
        <input type="checkbox" bind:checked={confirmed} />
        {t('approve.confirm_size')}
      </label>
    {/if}

    {#if reauthMethods}
      <ReauthPrompt
        methods={reauthMethods}
        ondone={send}
        oncancel={() => (reauthMethods = null)}
      />
    {/if}

    <div class="actions main-action">
      <button type="button" disabled={!canApprove} onclick={send}>{t('approve.approve')}</button>
    </div>
  {/if}

  {#if problem}
    <ProblemCard {problem} level={3} />
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
    color: var(--color-muted);
  }
  .confirm {
    display: flex;
    gap: 0.5rem;
    align-items: center;
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
