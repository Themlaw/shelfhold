<script lang="ts">
  // The "À classer" screen (EF-USR-015, EF-USR-006): the Works that wait, filters that only narrow
  // what is shown (no request, no change to the batch), an age selector per Work and per Edition
  // alone, and "Classer tout en lot" with the confirmation typed again, on a fresh session (the
  // password is asked in place). A role that is not the admin's is told so and sees no list. Everything shown comes from the
  // sources: it is text, never markup. A raised age shows the plan of the files to withdraw and
  // applies none of it (EF-BIB-037): withdrawing is its own explicit action.
  import { onMount } from 'svelte';
  import { rateEdition, rateWork, toRateList } from '#lib/api/index.ts';
  import type { ToRateItem, WithdrawalOut } from '#lib/api/types.gen.ts';
  import AgePicker from '#lib/components/AgePicker.svelte';
  import BulkBar from '#lib/components/BulkBar.svelte';
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
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';

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

  function resetFilters(): void {
    stateFilter = '';
    textFilter = '';
  }

  const shown = $derived(
    items.filter(
      (item) =>
        (stateFilter === '' || item.state === stateFilter) &&
        item.title.toLocaleLowerCase().includes(textFilter.trim().toLocaleLowerCase())
    )
  );

</script>

<svelte:head>
  <title>{t('toRate.title')}</title>
</svelte:head>

<main class="page">
  <div class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{t('toRate.title')}</h1>
    </div>
  </div>

  {#if signedOut}
    <Alert tone="danger" live="assertive" title={t('toRate.signed_out')} />
  {:else if forbidden}
    <Alert tone="danger" live="assertive" title={t('toRate.forbidden')} />
  {:else}
    <div class="stack-lg">
      {#if reauthMethods}
        <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
      {/if}

      {#if message}
        <Alert
          tone={failed ? 'danger' : 'success'}
          live={failed ? 'assertive' : 'polite'}
          title={message}
        />
      {/if}

      {#if plan.length > 0}
        <section aria-labelledby="plan-title" class="panel plan">
          <h2 id="plan-title">{t('toRate.plan_title', { count: plan.length })}</h2>
          <p>{t('toRate.plan_note')}</p>
          <ul>
            {#each plan as step (step.edition_id + step.shelf_id)}
              <li>{t('toRate.plan_line', { edition: step.edition_id, shelf: step.shelf_id, reason: step.reason })}</li>
            {/each}
          </ul>
        </section>
      {/if}

      {#if !loaded && !failed}
        <div aria-busy="true">
          <p class="visually-hidden" role="status">{t('toRate.loading')}</p>
          <div class="loading">
            {#each { length: 3 }, index (index)}
              <div class="skeleton-row"><Skeleton /><Skeleton short /><Skeleton /></div>
            {/each}
          </div>
        </div>
      {:else if loaded && items.length === 0}
        <EmptyState title={t('toRate.none')} />
      {:else if items.length > 0}
        <div class="filters">
          <Field label={t('toRate.filter_state')}>
            {#snippet children(control)}
              <Select {...control} bind:value={stateFilter}>
                <option value="">{t('toRate.filter_state_all')}</option>
                {#each Object.keys(STATES) as state (state)}
                  <option value={state}>{stateLabel(state)}</option>
                {/each}
              </Select>
            {/snippet}
          </Field>
          <Field label={t('toRate.filter_text')}>
            {#snippet children(control)}
              <Input
                {...control}
                type="search"
                bind:value={textFilter}
                autocomplete="off"
                spellcheck="false"
              />
            {/snippet}
          </Field>
        </div>
        {#if shown.length === 0}
          <EmptyState variant="filtered" title={t('toRate.filter_none')}>
            {#snippet actions()}
              <Button onclick={resetFilters}>{t('toRate.filter_reset')}</Button>
            {/snippet}
          </EmptyState>
        {/if}
        <ul class="entries works" role="list">
          {#each shown as item (item.work_id)}
            <li>
              <div class="head">
                <strong class="title">{item.title}</strong>
                <span class="muted">{stateLabel(item.state)}</span>
              </div>
              {#each item.suggestions ?? [] as suggestion (suggestion.edition_id + suggestion.provenance)}
                <div class="rating">
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
                  <div class="btn-row">
                    <Button
                      size="sm"
                      disabled={!editionAge[suggestion.edition_id]}
                      onclick={() => rateOneEdition(suggestion.edition_id)}
                    >
                      {t('toRate.edition_rate')}
                    </Button>
                  </div>
                </div>
              {/each}
              {#if item.state !== 'suggestion_above'}
                <div class="rating">
                  <AgePicker
                    name={`age-${item.work_id}`}
                    legend={t('toRate.age_for', { title: item.title })}
                    bind:value={chosen[item.work_id]}
                  />
                  <div class="btn-row">
                    <Button
                      variant="primary"
                      size="sm"
                      disabled={!chosen[item.work_id]}
                      onclick={() => rateOne(item)}
                    >
                      {t('toRate.rate')}
                    </Button>
                  </div>
                </div>
              {/if}
            </li>
          {/each}
        </ul>

        <BulkBar
          items={shown}
          stateFilter={stateFilter}
          textFilter={textFilter}
          onchanged={() => load()}
        />
      {/if}
    </div>
  {/if}
</main>

<style>
  .loading {
    display: grid;
    gap: var(--space-5);
  }
  .skeleton-row {
    display: grid;
    gap: var(--space-2);
  }
  .plan {
    display: grid;
    gap: var(--space-3);
  }
  .plan > h2,
  .plan > p,
  .plan > ul {
    margin: 0;
  }
  .plan li {
    overflow-wrap: anywhere;
  }
  /* Under 600 px the filters stack: each field is as wide as the screen at most. */
  .filters {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 16rem), 1fr));
    gap: var(--space-3) var(--space-5);
    align-items: end;
  }
  /* side by side, the second field is not stacked under the first */
  .filters :global(.field + .field) {
    margin-top: 0;
  }
  .works > li {
    display: grid;
    gap: var(--space-4);
    padding: var(--space-4) 0;
    min-width: 0;
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: var(--space-1) var(--space-4);
    overflow-wrap: anywhere;
  }
  .head > * {
    min-width: 0;
  }
  .title {
    font-size: var(--step-1);
  }
  .rating {
    display: grid;
    gap: var(--space-3);
    justify-items: start;
    min-width: 0;
  }
  .rating p {
    margin: 0;
    overflow-wrap: anywhere;
  }
</style>
