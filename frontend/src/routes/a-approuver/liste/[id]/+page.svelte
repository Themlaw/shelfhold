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
  import { t, type MessageKey } from '#lib/i18n/index.ts';
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

<main>
  {#if !loaded}
    <h1>{t('lists.review_title')}</h1>
    <p>{t('home.loading')}</p>
  {:else if problem || !review}
    <h1>{t('lists.review_title')}</h1>
    {#if problem}<ProblemCard {problem} />{/if}
  {:else}
    <h1>{review.name}</h1>
    <p class="summary">
      {t('lists.counts', { in_library: review.in_library, requested: review.requested })}
    </p>
    {#if review.annotated > 0}
      <p class="warning" role="status">{t('lists.annotated_warning', { count: review.annotated })}</p>
    {/if}
    {#if review.user_role === 'child'}
      <p class="hint">{t('lists.child_note')}</p>
    {/if}
    {#if review.items.length > 0 && review.items.every((i) => i.text === null)}
      <p class="hint">{t('lists.private_adult')}</p>
    {/if}

    <ol>
      {#each review.items as item (item.position)}
        <li>
          <span class="line">{item.text ?? t('approve.masked')}</span>
          {#if item.in_library}
            <span class="badge">{t('lists.outcome.in_library')}</span>
          {:else}
            <span class="badge">{t(STATES[item.request_state ?? ''] ?? 'lists.state.pending')}</span>
          {/if}
          {#if item.annotated}<span class="badge warn">{t('lists.annotated')}</span>{/if}
          {#if item.needs_age}<span class="badge">{t('approve.needs_age')}</span>{/if}
          {#if open(item)}
            <button type="button" onclick={() => (selected = item)}>{t('lists.decide')}</button>
          {/if}
        </li>
      {/each}
    </ol>

    {#if selected}
      {#key selected.position}
        <ApproveSheet item={asQueueItem(selected)} onapproved={decided} />
      {/key}
      <div class="reject">
        <label>
          {t('approve.reason')}
          <input bind:value={reason} />
        </label>
        <button type="button" disabled={reason.trim() === ''} onclick={doReject}>
          {t('approve.reject')}
        </button>
        <button type="button" onclick={doNotFound}>{t('approve.not_found')}</button>
        <button type="button" onclick={close}>{t('approve.close')}</button>
      </div>
      {#if decisionProblem}
        <ProblemCard problem={decisionProblem} level={3} />
      {/if}
    {/if}
  {/if}
  <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
  <p><a href="/a-approuver/liste">{t('lists.back_lists')}</a></p>
</main>

<style>
  main {
    max-width: 42rem;
    margin: 0 auto;
    padding: 1rem;
  }
  ol {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  li {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--color-rule);
  }
  .line {
    overflow-wrap: anywhere;
  }
  .summary,
  .hint {
    color: var(--color-muted);
  }
  .badge {
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    border: 1px solid var(--color-rule);
  }
  .warning,
  .warn {
    border: 1px solid var(--color-rule);
  }
  .warning {
    padding: 0.5rem 0.75rem;
    border-radius: 0.5rem;
  }
  .reject {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    align-items: end;
    margin-top: 1rem;
  }
</style>
