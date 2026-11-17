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
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, problemOf } from '#lib/triage.ts';

  const ME = '';
  let children = $state<ProfileOut[]>([]);
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
    children = (profiles.data?.items ?? []).filter((p) => p.role === 'child');
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

<main>
  <h1>{t('lists.title')}</h1>
  <p class="intro">{t('lists.intro')}</p>

  <form onsubmit={submit}>
    {#if children.length > 0}
      <label>
        {t('lists.for')}
        <select bind:value={target}>
          <option value={ME}>{t('lists.for_me')}</option>
          {#each children as child (child.id)}
            <option value={child.id}>{child.username}</option>
          {/each}
        </select>
      </label>
      {#if target !== ME}<p class="hint">{t('lists.child_note')}</p>{/if}
    {/if}
    <label>
      {t('lists.name')}
      <input bind:value={name} maxlength="200" autocomplete="off" />
    </label>
    <label>
      {t('lists.file')}
      <input type="file" accept=".txt,text/plain" onchange={readFile} />
    </label>
    <label>
      {t('lists.text')}
      <textarea bind:value={text} rows="8" maxlength="65536" spellcheck="false"></textarea>
    </label>
    <button type="submit" disabled={busy || text.trim() === ''}>{t('lists.submit')}</button>
  </form>

  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} ondone={send} oncancel={() => (reauthMethods = null)} />
  {/if}
  {#if actionProblem}
    <ProblemCard problem={actionProblem} level={2} />
  {/if}

  {#if done}
    <section class="done" role="status" aria-label={t('lists.done_label')}>
      <p>{t('lists.done', { in_library: done.in_library, requested: done.requested })}</p>
      {#if done.annotated > 0}
        <p class="warning">{t('lists.annotated_warning', { count: done.annotated })}</p>
      {/if}
      <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
      <a href={`/a-approuver/liste/${done.id}`}>{t('lists.open_review')}</a>
    </section>
  {/if}

  <h2>{t('lists.recent')}</h2>
  {#if !loaded}
    <p>{t('home.loading')}</p>
  {:else if problem}
    <ProblemCard {problem} level={3} />
  {:else if batches.length === 0}
    <p class="empty">{t('lists.recent_empty')}</p>
  {:else}
    <ul>
      {#each batches as batch (batch.id)}
        <li>
          <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
          <a href={`/a-approuver/liste/${batch.id}`}>{batch.name}</a>
          <span class="who">{t('lists.counts', { in_library: batch.in_library, requested: batch.requested })}</span>
        </li>
      {/each}
    </ul>
  {/if}
  <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
  <p><a href="/a-approuver">{t('lists.back_queue')}</a></p>
</main>

<style>
  main {
    max-width: 42rem;
    margin: 0 auto;
    padding: 1rem;
  }
  form {
    display: grid;
    gap: 0.75rem;
  }
  label {
    display: grid;
    gap: 0.25rem;
  }
  textarea,
  input,
  select {
    max-width: 100%;
    box-sizing: border-box;
  }
  .hint,
  .who {
    color: var(--color-muted);
  }
  .warning {
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--color-rule);
    border-radius: 0.5rem;
  }
  ul {
    list-style: none;
    padding: 0;
  }
  li {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--color-rule);
  }
</style>
