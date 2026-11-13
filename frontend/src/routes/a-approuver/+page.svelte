<script lang="ts">
  // "À approuver" (EF-USR-010): what waits for the admin. The server gives this list to the admin
  // alone (403 for anyone else, which is shown, never an empty list) and withholds the title of a
  // private request of an adult: the screen says "Titre masqué" and never shows an id instead.
  import { onMount } from 'svelte';
  import { notFound, queue as loadQueue, reject } from '#lib/api/index.ts';
  import type { QueueItemOut } from '#lib/api/types.gen.ts';
  import ApproveSheet from '#lib/components/ApproveSheet.svelte';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  let items = $state<QueueItemOut[]>([]);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);
  let selected = $state<QueueItemOut | null>(null);
  let decisionProblem = $state<ProblemView | null>(null);
  let reason = $state('');

  const ROLES: Record<string, MessageKey> = {
    child: 'approve.role.child',
    member: 'approve.role.member',
    admin: 'approve.role.admin'
  };

  async function load(): Promise<void> {
    const { data, error } = await loadQueue();
    if (error !== undefined) {
      problem = problemView(error);
      items = [];
    } else {
      problem = null;
      items = data?.items ?? [];
    }
    loaded = true;
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

  /** Refuse or mark not found: the server's refusal is shown and the request stays in the queue. */
  async function decide(send: (id: string) => Promise<{ error?: unknown }>): Promise<void> {
    if (!selected) return;
    const { error } = await send(selected.id);
    if (error !== undefined) {
      decisionProblem = problemView(error);
      return;
    }
    await decided();
  }

  const doReject = () =>
    decide((id) =>
      reject({ headers: { 'X-CSRF-Token': csrfToken() }, path: { request_id: id }, body: { reason } })
    );
  const doNotFound = () =>
    decide((id) =>
      notFound({ headers: { 'X-CSRF-Token': csrfToken() }, path: { request_id: id } })
    );

  onMount(load);
</script>

<svelte:head>
  <title>{t('approve.title')}</title>
</svelte:head>

<main>
  <h1>{t('approve.title')}</h1>

  {#if !loaded}
    <p>{t('home.loading')}</p>
  {:else if problem}
    <ProblemCard {problem} />
  {:else if items.length === 0}
    <p class="empty">{t('approve.empty')}</p>
  {:else}
    <ul>
      {#each items as item (item.id)}
        <li>
          <button type="button" onclick={() => (selected = item)}>
            {item.title ?? t('approve.masked')}
          </button>
          <span class="who">{t(ROLES[item.requester_role] ?? 'approve.role.member')}</span>
          {#if item.needs_age}<span class="badge">{t('approve.needs_age')}</span>{/if}
        </li>
      {/each}
    </ul>
  {/if}

  {#if selected}
    {#key selected.id}
      <ApproveSheet item={selected} onapproved={decided} />
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
</main>

<style>
  main {
    max-width: 42rem;
    margin: 0 auto;
    padding: 1rem;
  }
  ul {
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
  .who {
    color: var(--color-muted);
  }
  .badge {
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    border: 1px solid var(--color-rule);
  }
  .reject {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    align-items: end;
    margin-top: 1rem;
  }
</style>
