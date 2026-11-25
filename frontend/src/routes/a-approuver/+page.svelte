<script lang="ts">
  // "À approuver" (EF-USR-010): what waits for the admin. The server gives this list to the admin
  // alone (403 for anyone else, which is shown, never an empty list) and withholds the title of a
  // private request of an adult: the screen says "Titre masqué" and never shows an id instead.
  import { onMount } from 'svelte';
  import { notFound, queue as loadQueue, reject } from '#lib/api/index.ts';
  import type { QueueItemOut } from '#lib/api/types.gen.ts';
  import ApproveSheet from '#lib/components/ApproveSheet.svelte';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import DecisionPanel from '#lib/components/DecisionPanel.svelte';
  import { formatDate, t, type MessageKey } from '#lib/i18n/index.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Cover from '#lib/ui/atoms/Cover.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Dialog from '#lib/ui/molecules/Dialog.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
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

<main class="page">
  <div class="running-head">
    <span>{t('nav.requests')}</span>
    {#if loaded && !problem}<span class="running-head__folio">{t('approve.count', { count: items.length })}</span>{/if}
  </div>
  <div class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{t('approve.title')}</h1>
      <p class="lede">{t('approve.lede')}</p>
    </div>
    <div class="page-head__actions">
      <Button href="/a-approuver/liste" icon="list">{t('approve.import_list')}</Button>
    </div>
  </div>

  {#if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('approve.loading')}</p>
      <ul class="entries" role="list">
        {#each { length: 4 }, index (index)}
          <li class="entry"><Skeleton shape="cover" /><div class="entry__text"><Skeleton /><Skeleton short /></div></li>
        {/each}
      </ul>
    </div>
  {:else if problem}
    <ProblemCard {problem} />
  {:else if items.length === 0}
    <EmptyState title={t('approve.empty')} />
  {:else}
    <ul class="entries" role="list">
      {#each items as item (item.id)}
        <li class="entry">
          <Cover title={item.title ?? t('approve.masked')} seed={item.id} size="sm" />
          <div class="entry__text">
            <p class="entry__title">{item.title ?? t('approve.masked')}</p>
            <p class="entry__meta">
              {t('approve.requested_on', {
                role: t(ROLES[item.requester_role] ?? 'approve.role.member'),
                date: formatDate(item.created_at, { day: 'numeric', month: 'long' })
              })}
              {#if item.needs_age}<Badge tone="warning" icon={null}>{t('approve.needs_age')}</Badge>{/if}
            </p>
          </div>
          <Button size="sm" aria-label={t('approve.review_title', { title: item.title ?? t('approve.masked') })} onclick={() => (selected = item)}>
            {t('home.approve_review')}
          </Button>
        </li>
      {/each}
    </ul>
  {/if}
</main>

{#if selected}
  <Dialog open={true} title={t('approve.title')} description={t('approve.dialog_help')} onclose={close}>
    {#key selected.id}
      <ApproveSheet item={selected} onapproved={decided} />
    {/key}
    <DecisionPanel bind:reason onreject={doReject} onnotfound={doNotFound} onclose={close} />
    {#if decisionProblem}
      <ProblemCard problem={decisionProblem} level={3} />
    {/if}
  </Dialog>
{/if}

<style>
  .entry {
    display: grid;
    grid-template-columns: 3.5rem minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--space-4);
    padding-block: var(--space-3);
  }
  .entry__title {
    margin: 0;
    font: 600 var(--step-1) / var(--leading-tight) var(--font-text);
    overflow-wrap: anywhere;
  }
  .entry__meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin: var(--space-1) 0 0;
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  @media (max-width: 40rem) {
    .entry {
      grid-template-columns: 3rem minmax(0, 1fr);
    }
    .entry > :global(.btn) {
      grid-column: 1 / -1;
    }
  }
</style>
