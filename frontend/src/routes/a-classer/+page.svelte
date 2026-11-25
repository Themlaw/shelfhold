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
  import Badge, { type BadgeTone } from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

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

  const shown = $derived(
    items.filter(
      (item) =>
        (stateFilter === '' || item.state === stateFilter) &&
        item.title.toLocaleLowerCase().includes(textFilter.trim().toLocaleLowerCase())
    )
  );


  const STATE_TONE: Record<string, BadgeTone> = {
    rating_pending: 'warning',
    follow_suspended: 'danger',
    suggestion_above: 'accent'
  };
</script>

<svelte:head>
  <title>{t('toRate.title')}</title>
</svelte:head>

<main class="page page--read">
  <PageHead title={t('toRate.title')} />

  {#if signedOut}
    <Alert tone="warning" title={t('toRate.signed_out')} live="assertive" />
  {:else if forbidden}
    <Alert tone="danger" title={t('toRate.forbidden')} live="assertive" />
  {:else}
    <div class="stack-lg">
      {#if reauthMethods}
        <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
      {/if}

      {#if message}
        <Alert tone={failed ? 'danger' : 'success'} title={message} live={failed ? 'assertive' : 'polite'} />
      {/if}

      {#if plan.length > 0}
        <section aria-labelledby="plan-title" class="plan">
          <div class="section-head"><h2 id="plan-title">{t('toRate.plan_title', { count: plan.length })}</h2></div>
          <p>{t('toRate.plan_note')}</p>
          <ul class="entries">
            {#each plan as step (step.edition_id + step.shelf_id)}
              <li>{t('toRate.plan_line', { edition: step.edition_id, shelf: step.shelf_id, reason: step.reason })}</li>
            {/each}
          </ul>
        </section>
      {/if}

      {#if !loaded}
        <div aria-busy="true">
          <p class="visually-hidden" role="status">{t('toRate.loading')}</p>
          <Skeleton shape="block" /><Skeleton /><Skeleton short />
        </div>
      {:else if items.length === 0}
        <EmptyState title={t('toRate.none')} />
      {:else}
        <div class="filters">
          <Field label={t('toRate.filter_state')}>
            {#snippet children(control)}
              <Select
                {...control}
                bind:value={stateFilter}
                options={[
                  { value: '', label: t('toRate.filter_state_all') },
                  ...Object.keys(STATES).map((state) => ({ value: state, label: stateLabel(state) }))
                ]}
              />
            {/snippet}
          </Field>
          <Field label={t('toRate.filter_text')}>
            {#snippet children(control)}
              <Input {...control} type="search" bind:value={textFilter} autocomplete="off" spellcheck={false} />
            {/snippet}
          </Field>
        </div>
        {#if shown.length === 0}
          <EmptyState variant="filtered" title={t('toRate.filter_none')} />
        {/if}
        <ul class="works">
          {#each shown as item (item.work_id)}
            <li class="work">
              <div class="head">
                <strong>{item.title}</strong>
                <Badge tone={STATE_TONE[item.state] ?? 'neutral'}>{stateLabel(item.state)}</Badge>
              </div>
              {#each item.suggestions ?? [] as suggestion (suggestion.edition_id + suggestion.provenance)}
                <div class="suggestion">
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
                  <div>
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
                <AgePicker
                  name={`age-${item.work_id}`}
                  legend={t('toRate.age_for', { title: item.title })}
                  bind:value={chosen[item.work_id]}
                />
                <div>
                  <Button variant="primary" disabled={!chosen[item.work_id]} onclick={() => rateOne(item)}>
                    {t('toRate.rate')}
                  </Button>
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
  .works {
    list-style: none;
    margin: var(--space-4) 0 0;
    padding: 0;
    display: grid;
    gap: var(--space-4);
  }
  .work {
    display: grid;
    gap: var(--space-3);
    justify-items: start;
    min-width: 0;
    padding: var(--space-4) var(--space-5);
    border: var(--rule-hair) solid var(--color-rule);
    border-radius: var(--radius-md);
    background: var(--color-surface);
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-1) var(--space-3);
    inline-size: 100%;
    overflow-wrap: anywhere;
  }
  .head > * {
    min-width: 0;
  }
  .suggestion {
    display: grid;
    gap: var(--space-2);
    justify-items: start;
    padding: var(--space-3);
    background: var(--color-surface-2);
    border-radius: var(--radius-sm);
    inline-size: 100%;
    box-sizing: border-box;
  }
  .head :global(.badge) {
    white-space: normal;
    max-inline-size: 100%;
  }
  .suggestion p {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .plan li {
    padding: var(--space-2) 0;
    overflow-wrap: anywhere;
  }
  .filters {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 14rem), 1fr));
    gap: var(--space-3) var(--space-5);
  }
</style>
