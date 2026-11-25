<script lang="ts">
  // One imported list, on one screen (EF-USR-020): every line in the order of the file, what is
  // already in the library, and each request to decide with the ordinary approval (an age is still
  // asked for a child's). A line that asks for a precise annotated edition carries a warning, not a
  // refusal. The server gives the batch to an admin, or to its owner; another's is a plain "not
  // found". The lines of another adult's private list are not sent: only the counts.
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import { notFound, readRequestList, reject } from '#lib/api/index.ts';
  import type { ListItemOut, ListReviewOut, QueueItemOut } from '#lib/api/types.gen.ts';
  import ApproveSheet from '#lib/components/ApproveSheet.svelte';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import DecisionPanel from '#lib/components/DecisionPanel.svelte';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import Dialog from '#lib/ui/molecules/Dialog.svelte';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  const id = $derived(page.params.id ?? '');
  let review = $state<ListReviewOut | null>(null);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);
  let selected = $state<ListItemOut | null>(null);
  let decisionProblem = $state<ProblemView | null>(null);
  let reason = $state('');

  const STATES: Record<string, MessageKey> = {
    pending: 'lists.state.pending',
    approved: 'lists.state.approved',
    arrived: 'lists.state.arrived',
    rejected: 'lists.state.rejected',
    not_found: 'lists.state.not_found',
    cancelled: 'lists.state.cancelled',
    to_buy: 'lists.state.to_buy',
    borrow_elsewhere: 'lists.state.borrow_elsewhere'
  };

  const TONES: Record<string, 'success' | 'warning' | 'danger' | 'neutral'> = {
    approved: 'success',
    arrived: 'success',
    rejected: 'danger',
    not_found: 'danger',
    to_buy: 'warning',
    borrow_elsewhere: 'warning'
  };

  async function load(): Promise<void> {
    const { data, error } = await readRequestList({ path: { import_id: id } });
    if (error !== undefined || !data) {
      problem = problemView(error);
      review = null;
    } else {
      problem = null;
      review = data;
    }
    loaded = true;
  }

  /** What the approval sheet reads of a line: the same shape as a line of the queue. */
  function asQueueItem(item: ListItemOut): QueueItemOut {
    return {
      id: item.request_id ?? '',
      title: item.text,
      target_kind: item.isbn && !item.text ? 'isbn' : 'free_text',
      visibility: 'household',
      requester_id: review?.user_id ?? '',
      requester_role: review?.user_role ?? 'member',
      needs_age: item.needs_age,
      created_at: review?.created_at ?? ''
    };
  }

  function close(): void {
    selected = null;
    reason = '';
    decisionProblem = null;
  }

  async function decided(): Promise<void> {
    close();
    await load();
  }

  async function decide(send: (requestId: string) => Promise<{ error?: unknown }>): Promise<void> {
    if (!selected?.request_id) return;
    const { error } = await send(selected.request_id);
    if (error !== undefined) {
      decisionProblem = problemView(error);
      return;
    }
    await decided();
  }

  const doReject = () =>
    decide((requestId) =>
      reject({ headers: { 'X-CSRF-Token': csrfToken() }, path: { request_id: requestId }, body: { reason } })
    );
  const doNotFound = () =>
    decide((requestId) =>
      notFound({ headers: { 'X-CSRF-Token': csrfToken() }, path: { request_id: requestId } })
    );

  const open = (item: ListItemOut) => item.request_id !== null && item.request_state === 'pending';

  onMount(load);
</script>

<svelte:head>
  <title>{review?.name ?? t('lists.review_title')}</title>
</svelte:head>

<main class="page page--read">
  <div class="running-head">
    <span>{t('lists.title')}</span>
  </div>
  {#if !loaded}
    <div class="page-head"><h1 class="page-title">{t('lists.review_title')}</h1></div>
    <div aria-busy="true"><p class="visually-hidden" role="status">{t('home.loading')}</p><Skeleton /><Skeleton /><Skeleton short /></div>
  {:else if problem || !review}
    <div class="page-head"><h1 class="page-title">{t('lists.review_title')}</h1></div>
    {#if problem}<ProblemCard {problem} />{/if}
  {:else}
    <div class="page-head">
      <div class="page-head__text">
        <h1 class="page-title">{review.name}</h1>
        <p class="lede">{t('lists.counts', { in_library: review.in_library, requested: review.requested })}</p>
      </div>
    </div>
    <div class="notes">
      {#if review.annotated > 0}
        <Alert tone="warning" title={t('lists.annotated_warning', { count: review.annotated })} live="polite" />
      {/if}
      {#if review.user_role === 'child'}
        <Alert tone="info" title={t('lists.child_note')} />
      {/if}
      {#if review.items.length > 0 && review.items.every((i) => i.text === null)}
        <Alert tone="info" title={t('lists.private_adult')} />
      {/if}
    </div>

    <ol class="entries" role="list">
      {#each review.items as item (item.position)}
        <li class="line">
          <span class="line__no folio">{item.position}</span>
          <span class="line__text">{item.text ?? t('approve.masked')}</span>
          <span class="line__badges">
            {#if item.in_library}
              <Badge tone="success">{t('lists.outcome.in_library')}</Badge>
            {:else}
              <Badge tone={TONES[item.request_state ?? ''] ?? 'neutral'}>{t(STATES[item.request_state ?? ''] ?? 'lists.state.pending')}</Badge>
            {/if}
            {#if item.annotated}<Badge tone="warning">{t('lists.annotated')}</Badge>{/if}
            {#if item.needs_age}<Badge tone="warning" icon={null}>{t('approve.needs_age')}</Badge>{/if}
          </span>
          {#if open(item)}
            <Button size="sm" onclick={() => (selected = item)}>{t('lists.decide')}</Button>
          {/if}
        </li>
      {/each}
    </ol>
  {/if}
</main>

{#if selected}
  <Dialog open={true} title={t('lists.decide')} description={t('approve.dialog_help')} onclose={close}>
    {#key selected.position}
      <ApproveSheet item={asQueueItem(selected)} onapproved={decided} />
    {/key}
    <DecisionPanel bind:reason onreject={doReject} onnotfound={doNotFound} onclose={close} />
    {#if decisionProblem}
      <ProblemCard problem={decisionProblem} level={3} />
    {/if}
  </Dialog>
{/if}

<style>
  .notes {
    display: grid;
    gap: var(--space-3);
    margin-bottom: var(--space-5);
  }
  .line {
    display: grid;
    grid-template-columns: 2rem minmax(0, 1fr) auto auto;
    align-items: center;
    gap: var(--space-3);
    padding-block: var(--space-2);
  }
  .line__text {
    overflow-wrap: anywhere;
  }
  .line__badges {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  @media (max-width: 40rem) {
    .line {
      grid-template-columns: 2rem minmax(0, 1fr);
    }
    .line__badges,
    .line > :global(.btn) {
      grid-column: 2;
      justify-self: start;
    }
  }
</style>
