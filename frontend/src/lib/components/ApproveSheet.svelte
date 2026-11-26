<script lang="ts">
  // "À approuver" (EF-USR-010): one gesture to approve, with the age selector the server asks for
  // a child's request, the size summary and a confirmation beyond 100 Units or 5 GiB. The sheet
  // decides nothing: the server refuses an approval with no age, with no confirmation or from a
  // role that has no right to it, and says why; the button only follows what the preview said.
  import { onMount } from 'svelte';
  import { approve, approval as fetchApproval } from '#lib/api/index.ts';
  import type { ApprovalOut, QueueItemOut } from '#lib/api/types.gen.ts';
  import AgePicker from '#lib/components/AgePicker.svelte';
  import ProblemAlert from '#lib/ui/molecules/ProblemAlert.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { formatBytes } from '#lib/bytes.ts';
  import { t } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Checkbox from '#lib/ui/atoms/Checkbox.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
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
  /** A warning from the server, never a refusal: the age is above the child's ceiling. */
  let above = $state(false);

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
    above = data.above_requester_ceiling;
    age = decided(data);
  });

  // The server says whether the age being chosen is above the ceiling of the child who asked.
  async function checkAge(chosen: Age | null) {
    const { age: years, x } = rating(chosen);
    if (years === null) return;
    const { data } = await fetchApproval({
      path: { request_id: item.id },
      query: { age: years, x }
    });
    if (data !== undefined) above = data.above_requester_ceiling;
  }
  $effect(() => {
    if (preview !== null) void checkAge(age);
  });

  const needsAge = $derived(preview?.needs_age ?? item.needs_age);
  const missingAge = $derived(needsAge && age === null);
  const canApprove = $derived(!busy && (!preview?.confirmation_required || confirmed));

  /** The way the server reads a tier: X is the top tier with the X flag. */
  function rating(chosen: Age | null): { age: number | null; x: boolean } {
    if (chosen === null) return { age: null, x: false };
    if (chosen === 'X') return { age: 18, x: true };
    return { age: Number.parseInt(chosen, 10), x: false };
  }

  async function send(): Promise<void> {
    if (missingAge || !canApprove) return;
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
  <h3 class="approve-sheet__title">{item.title ?? t('approve.masked')}</h3>

  {#if preview}
    <p class="approve-sheet__summary">
      {t('approve.size_summary', {
        volumes: preview.units,
        bytes: formatBytes(preview.size_bytes ?? 0)
      })}
      {#if preview.units_minimum}({t('approve.more_to_come')}){/if}
    </p>

    {#if needsAge}
      <AgePicker name={`age-${item.id}`} legend={t('approve.age_for_child')} bind:value={age} />
      {#if age === null}
        <p class="hint">{t('approve.choose_age')}</p>
      {/if}
      {#if above}
        <Alert tone="warning" title={t('approve.above_ceiling')} live="polite" />
      {/if}
    {/if}

    {#if preview.confirmation_required}
      <Checkbox bind:checked={confirmed} label={t('approve.confirm_size')} />
    {/if}

    {#if preview.non_family_source_into_family_shelf}
      <Alert tone="warning" title={t('approve.non_family_source')} live="polite" />
    {/if}

    {#if reauthMethods}
      <ReauthPrompt
        methods={reauthMethods}
        ondone={send}
        oncancel={() => (reauthMethods = null)}
      />
    {/if}

    <div class="btn-row">
      <Button variant="primary" icon="check" disabled={!canApprove} loading={busy} onclick={send}>{t('approve.approve')}</Button>
    </div>
  {:else if !problem}
    <div aria-busy="true"><Skeleton /><Skeleton short /></div>
  {/if}

  {#if problem}
    <ProblemAlert {problem} level={3} />
  {/if}
</section>

<style>
  .approve-sheet {
    display: grid;
    gap: var(--space-3);
  }
  .approve-sheet__title {
    margin: 0;
    font-size: var(--step-1);
    overflow-wrap: anywhere;
  }
  .approve-sheet__summary,
  .hint {
    margin: 0;
    color: var(--color-muted);
  }
</style>
