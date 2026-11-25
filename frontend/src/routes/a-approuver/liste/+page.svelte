<script lang="ts">
  // Import a reading list (EF-USR-020): one title per line, an ISBN if there is one. The server
  // reads the text as untrusted, resolves it in the catalogue only (nothing is asked of a source),
  // puts what the profile is shown on a reading shelf and makes a request of the rest. For a child
  // it is the admin's decision (a fresh session is asked for in place), and every request of hers
  // still asks for an age at approval. This screen sends the text and draws the answer; the whole
  // batch is read, and decided, on one screen (`./<id>`).
  import { onMount } from 'svelte';
  import {
    importRequestList,
    importRequestListForChild,
    listProfiles,
    listRequestLists
  } from '#lib/api/index.ts';
  import type { ListImportOut, ListSummaryOut, ProfileOut } from '#lib/api/types.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { formatNumber, t } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Textarea from '#lib/ui/atoms/Textarea.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, problemOf } from '#lib/triage.ts';

  const ME = '';
  let kids = $state<ProfileOut[]>([]);
  let batches = $state<ListSummaryOut[]>([]);
  let loaded = $state(false);
  let problem = $state<ProblemView | null>(null);
  let actionProblem = $state<ProblemView | null>(null);
  let reauthMethods = $state<string[] | null>(null);
  let busy = $state(false);
  let target = $state(ME);
  let name = $state('');
  let text = $state('');
  let done = $state<ListImportOut | null>(null);

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  async function load(): Promise<void> {
    const [profiles, lists] = await Promise.all([listProfiles(), listRequestLists()]);
    if (lists.error !== undefined || !lists.data) {
      problem = problemView(lists.error);
    } else {
      problem = null;
      batches = lists.data.items;
    }
    // Only an admin may read the profiles (a member's screen simply has no "for" choice).
    kids = (profiles.data?.items ?? []).filter((p) => p.role === 'child');
    loaded = true;
  }

  async function readFile(event: Event): Promise<void> {
    const file = (event.currentTarget as HTMLInputElement).files?.[0];
    if (!file) return;
    text = await file.text();
    if (name.trim() === '') name = file.name.replace(/\.[^.]+$/, '');
  }

  async function send(): Promise<void> {
    busy = true;
    actionProblem = null;
    done = null;
    try {
      const body = { text, name: name.trim() === '' ? null : name.trim() };
      const { data, error } =
        target === ME
          ? await importRequestList({ headers: headers(), body })
          : await importRequestListForChild({ headers: headers(), path: { user_id: target }, body });
      if (error !== undefined || !data) {
        if (needsReauth(error)) {
          reauthMethods = problemOf(error).reauth_methods ?? ['password'];
        } else {
          reauthMethods = null;
          actionProblem = problemView(error);
        }
        return;
      }
      reauthMethods = null;
      done = data;
      text = '';
      name = '';
      await load();
    } finally {
      busy = false;
    }
  }

  function submit(event: SubmitEvent): void {
    event.preventDefault();
    void send();
  }

  onMount(load);
</script>

<svelte:head>
  <title>{t('lists.title')}</title>
</svelte:head>

<main class="page page--form">
  <div class="running-head">
    <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
    <a href="/a-approuver">{t('approve.title')}</a>
  </div>
  <div class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{t('lists.title')}</h1>
      <p class="lede">{t('lists.intro')}</p>
    </div>
  </div>

  <form class="import" onsubmit={submit}>
    {#if kids.length > 0}
      <Field label={t('lists.for')} hint={target !== ME ? t('lists.child_note') : undefined}>
        {#snippet children(control)}
          <Select
            {...control}
            bind:value={target}
            options={[{ value: ME, label: t('lists.for_me') }, ...kids.map((child) => ({ value: child.id, label: child.username }))]}
          />
        {/snippet}
      </Field>
    {/if}
    <Field label={t('lists.name')}>
      {#snippet children(control)}
        <Input {...control} bind:value={name} maxlength={200} autocomplete="off" />
      {/snippet}
    </Field>
    <Field label={t('lists.file')}>
      {#snippet children(control)}
        <input {...control} class="file" type="file" accept=".txt,text/plain" onchange={readFile} />
      {/snippet}
    </Field>
    <Field label={t('lists.text')} required>
      {#snippet children(control)}
        <Textarea {...control} bind:value={text} rows={8} maxlength={65536} spellcheck={false} />
      {/snippet}
    </Field>
    <div class="btn-row">
      <Button type="submit" variant="primary" icon="send" disabled={text.trim() === ''} loading={busy}>{t('lists.submit')}</Button>
    </div>
  </form>

  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} ondone={send} oncancel={() => (reauthMethods = null)} />
  {/if}
  {#if actionProblem}
    <ProblemCard problem={actionProblem} level={2} />
  {/if}

  {#if done}
    <div class="done">
      <Alert tone="success" title={t('lists.done', { in_library: done.in_library, requested: done.requested })} live="polite" headingLevel={2}>
        {#if done.annotated > 0}
          <p>{t('lists.annotated_warning', { count: done.annotated })}</p>
        {/if}
        {#snippet actions()}
          <Button href={`/a-approuver/liste/${done?.id}`} variant="primary" size="sm" iconEnd="right">{t('lists.open_review')}</Button>
        {/snippet}
      </Alert>
    </div>
  {/if}

  <section class="section" aria-labelledby="recent-lists">
    <div class="section-head">
      <h2 id="recent-lists">{t('lists.recent')}</h2>
      {#if loaded && batches.length > 0}<span class="folio">{formatNumber(batches.length)}</span>{/if}
    </div>
    {#if !loaded}
      <div aria-busy="true"><p class="visually-hidden" role="status">{t('home.loading')}</p><Skeleton /><Skeleton short /></div>
    {:else if problem}
      <ProblemCard {problem} level={3} />
    {:else if batches.length === 0}
      <EmptyState title={t('lists.recent_empty')} headingLevel={3} art={false} />
    {:else}
      <ul class="entries" role="list">
        {#each batches as batch (batch.id)}
          <li class="batch">
            <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
            <a class="standalone" href={`/a-approuver/liste/${batch.id}`}>{batch.name}</a>
            <span class="hint">{t('lists.counts', { in_library: batch.in_library, requested: batch.requested })}</span>
          </li>
        {/each}
      </ul>
    {/if}
  </section>
  <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
  <p class="back"><a class="standalone" href="/a-approuver">{t('lists.back_queue')}</a></p>
</main>

<style>
  .import {
    display: grid;
    gap: var(--space-4);
  }
  .file {
    inline-size: 100%;
  }
  .done {
    margin-top: var(--space-5);
  }
  .batch {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-1) var(--space-4);
    padding-block: var(--space-1);
  }
  .batch a {
    font: 600 var(--step-1) / var(--leading-tight) var(--font-text);
  }
  .back {
    margin-top: var(--space-6);
  }
</style>
