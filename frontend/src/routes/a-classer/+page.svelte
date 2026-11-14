<script lang="ts">
  // The "À classer" screen (EF-USR-015, EF-USR-006): the Works that wait, filters that only narrow
  // what is shown (no request, no change to the batch), an age selector per Work and per Edition
  // alone, and "Classer tout en lot" with the confirmation typed again, on a fresh session (the
  // password is asked in place). A role that is not the admin's is told so and sees no list. Everything shown comes from the
  // sources: it is text, never markup. A raised age shows the plan of the files to withdraw and
  // applies none of it (EF-BIB-037): withdrawing is its own explicit action.
  import { onMount } from 'svelte';
  import { rateBatch, rateEdition, rateWork, toRateList } from '#lib/api/index.ts';
  import type { ToRateItem, WithdrawalOut } from '#lib/api/types.gen.ts';
  import AgePicker from '#lib/components/AgePicker.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import {
    ageLabel,
    isForbidden,
    needsReauth,
    needsSession,
    problemOf,
    type Age
  } from '#lib/triage.ts';

  let items = $state<ToRateItem[]>([]);
  let loaded = $state(false);
  let signedOut = $state(false);
  let forbidden = $state(false);
  let stateFilter = $state('');
  let textFilter = $state('');
  let editionAge = $state<Record<string, Age | null>>({});
  let message = $state('');
  let failed = $state(false);
  let chosen = $state<Record<string, Age | null>>({});
  let plan = $state<WithdrawalOut[]>([]);
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  let batchAge = $state<Age | null>(null);
  let batchToken = $state<string | null>(null);
  let typed = $state('');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const STATES: Record<string, MessageKey> = {
    rating_pending: 'toRate.state.rating_pending',
    follow_suspended: 'toRate.state.follow_suspended',
    suggestion_above: 'toRate.state.suggestion_above'
  };

  const stateLabel = (state: string): string => {
    const key = STATES[state];
    return key ? t(key) : state;
  };

  function say(text: string, bad = false): void {
    message = text;
    failed = bad;
  }

  /** Run an action; a stale password asks for it in place and runs the action again after. */
  async function guarded(action: () => Promise<void>): Promise<void> {
    try {
      await action();
    } catch (error) {
      if (needsReauth(error)) {
        retry = action;
        reauthMethods = problemOf(error).reauth_methods ?? ['password'];
      } else if (needsSession(error)) {
        signedOut = true;
      } else if (isForbidden(error)) {
        forbidden = true;
        items = [];
      } else {
        say(t('toRate.error'), true);
      }
    }
  }

  async function load(): Promise<void> {
    await guarded(async () => {
      const { data } = await toRateList({ throwOnError: true });
      items = data.items;
      loaded = true;
    });
  }

  onMount(load);

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

  async function rateOne(item: ToRateItem): Promise<void> {
    const age = chosen[item.work_id];
    if (!age) return;
    await guarded(async () => {
      const { data } = await rateWork({
        headers: headers(),
        path: { work_id: item.work_id },
        body: { age },
        throwOnError: true
      });
      plan = data.withdrawals;
      say(t('toRate.rated', { title: item.title, age: t(ageLabel(age)) }));
      await load();
    });
  }

  async function rateOneEdition(edition: string): Promise<void> {
    const age = editionAge[edition];
    if (!age) return;
    await guarded(async () => {
      const { data } = await rateEdition({
        headers: headers(),
        path: { edition_id: edition },
        body: { age },
        throwOnError: true
      });
      plan = data.withdrawals;
      say(t('toRate.edition_rated', { age: t(ageLabel(age)) }));
      await load();
    });
  }

  const shown = $derived(
    items.filter(
      (item) =>
        (stateFilter === '' || item.state === stateFilter) &&
        item.title.toLocaleLowerCase().includes(textFilter.trim().toLocaleLowerCase())
    )
  );

  async function prepareBatch(): Promise<void> {
    const age = batchAge;
    if (!age) return;
    await guarded(async () => {
      const { data } = await toRateList({ query: { age }, throwOnError: true });
      batchToken = data.token;
      items = data.items;
      typed = '';
    });
  }

  async function runBatch(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const age = batchAge;
    if (!age || !batchToken || typed.trim() !== batchToken) return;
    const confirm = typed.trim();
    await guarded(async () => {
      const { data } = await rateBatch({
        headers: headers(),
        body: { age, confirm },
        throwOnError: true
      });
      plan = data.rated.flatMap((r) => r.withdrawals);
      say(t('toRate.batch_done', { count: data.rated.length, age: t(ageLabel(age)) }));
      batchToken = null;
      typed = '';
      await load();
    });
  }
</script>

<svelte:head>
  <title>{t('toRate.title')}</title>
</svelte:head>

<main>
  <h1>{t('toRate.title')}</h1>

  {#if signedOut}
    <p role="alert" class="err">{t('toRate.signed_out')}</p>
  {:else if forbidden}
    <p role="alert" class="err">{t('toRate.forbidden')}</p>
  {:else}
    {#if reauthMethods}
      <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
    {/if}

    {#if message}
      <p role={failed ? 'alert' : 'status'} class:err={failed}>{message}</p>
    {/if}

    {#if plan.length > 0}
      <section aria-labelledby="plan-title" class="plan">
        <h2 id="plan-title">{t('toRate.plan_title', { count: plan.length })}</h2>
        <p>{t('toRate.plan_note')}</p>
        <ul>
          {#each plan as step (step.edition_id + step.shelf_id)}
            <li>{t('toRate.plan_line', { edition: step.edition_id, shelf: step.shelf_id, reason: step.reason })}</li>
          {/each}
        </ul>
      </section>
    {/if}

    {#if loaded && items.length === 0}
      <p>{t('toRate.none')}</p>
    {:else if items.length > 0}
      <div class="filters">
        <label>
          {t('toRate.filter_state')}
          <select bind:value={stateFilter}>
            <option value="">{t('toRate.filter_state_all')}</option>
            {#each Object.keys(STATES) as state (state)}
              <option value={state}>{stateLabel(state)}</option>
            {/each}
          </select>
        </label>
        <label>
          {t('toRate.filter_text')}
          <input type="search" bind:value={textFilter} autocomplete="off" spellcheck="false" />
        </label>
      </div>
      {#if shown.length === 0}
        <p>{t('toRate.filter_none')}</p>
      {/if}
      <ul class="works">
        {#each shown as item (item.work_id)}
          <li>
            <div class="head">
              <strong>{item.title}</strong>
              <span class="muted">{stateLabel(item.state)}</span>
            </div>
            {#each item.suggestions ?? [] as suggestion (suggestion.edition_id + suggestion.provenance)}
              <p class="muted">
                {t('toRate.suggestion', {
                  edition: suggestion.edition_id,
                  age: suggestion.x ? 'X' : `${suggestion.age}+`,
                  provenance: suggestion.provenance
                })}
              </p>
              <AgePicker
                name={`age-edition-${suggestion.edition_id}`}
                legend={t('toRate.edition_age', { edition: suggestion.edition_id })}
                bind:value={editionAge[suggestion.edition_id]}
              />
              <button
                type="button"
                disabled={!editionAge[suggestion.edition_id]}
                onclick={() => rateOneEdition(suggestion.edition_id)}
              >
                {t('toRate.edition_rate')}
              </button>
            {/each}
            {#if item.state !== 'suggestion_above'}
            <AgePicker
              name={`age-${item.work_id}`}
              legend={t('toRate.age_for', { title: item.title })}
              bind:value={chosen[item.work_id]}
            />
            <button type="button" disabled={!chosen[item.work_id]} onclick={() => rateOne(item)}>
              {t('toRate.rate')}
            </button>
            {/if}
          </li>
        {/each}
      </ul>

      <section aria-labelledby="batch-title" class="batch">
        <h2 id="batch-title">{t('toRate.batch_title')}</h2>
        <p class="muted">{t('toRate.batch_scope', { count: items.length })}</p>
        <AgePicker name="age-batch" legend={t('toRate.batch_age')} bind:value={batchAge} />
        <button type="button" disabled={!batchAge} onclick={prepareBatch}>
          {t('toRate.batch_prepare')}
        </button>
        {#if batchToken}
          <form onsubmit={runBatch}>
            <p>{t('toRate.batch_confirm', { count: items.length, token: batchToken })}</p>
            <label for="batch-confirm">{t('toRate.batch_token')}</label>
            <input id="batch-confirm" bind:value={typed} autocomplete="off" spellcheck="false" />
            <button type="submit" disabled={typed.trim() !== batchToken}>
              {t('toRate.batch_run')}
            </button>
          </form>
        {/if}
      </section>
    {/if}
  {/if}
</main>

<style>
  main {
    max-width: 42rem;
    margin: 0 auto;
    padding: 3rem 1rem;
  }
  h1 {
    font-family: var(--font-display);
    font-weight: 400;
    font-style: italic;
    letter-spacing: -0.02em;
  }
  .works {
    list-style: none;
    margin: 1rem 0;
    padding: 0;
    display: grid;
    gap: 1rem;
  }
  .works li {
    display: grid;
    gap: 0.5rem;
    padding: 1rem;
    border: 1px solid var(--color-rule);
    border-radius: var(--radius-cover);
    background: var(--color-surface);
  }
  .head {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    overflow-wrap: anywhere;
  }
  .batch,
  .plan {
    display: grid;
    gap: 0.5rem;
    margin-top: 1.5rem;
  }
  form {
    display: grid;
    gap: 0.5rem;
  }
  .filters {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem 1rem;
    margin-top: 1rem;
  }
  .filters label {
    display: grid;
    gap: 0.25rem;
  }
  select,
  input,
  button {
    min-height: 2.75rem;
  }
  .muted {
    color: var(--color-muted);
  }
  .err {
    color: var(--color-err);
  }
</style>
