<script lang="ts">
  // The Kavita account of a member (EF-USR-012, D8): the key of their own account on each Kavita
  // destination open to them. The key goes in a password field, in the body of one request, and is
  // wiped from the field before the request leaves: it is never shown again, nowhere on the page,
  // never kept in storage, and the server answers with a fingerprint only. A child enters nothing
  // here: a parent does it for them.
  import { onMount } from 'svelte';
  import { client } from '#lib/api/client.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import SyncStatus from '#lib/components/SyncStatus.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { formatDateTime, hasMessage, t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { needsReauth, problemOf } from '#lib/triage.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import ErrorState from '#lib/ui/molecules/ErrorState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';

  type Choice = { id: string; shelf_name: string; target_name: string; linked: boolean };
  type Account = {
    shelf_destination_id: string;
    remote_user: string | null;
    token_fingerprint: string | null;
    verified_at: string | null;
    revoked_at: string | null;
    warnings?: string[];
  };
  type Form = { remote: string; key: string };
  type Outcome = { kind: 'linked' | 'admin' | 'revoked' } | { kind: 'refused'; problem: ProblemView; note: string | null };

  let me = $state<{ id: string; role: string } | null>(null);
  let choices = $state<Choice[]>([]);
  let accounts = $state<Record<string, Account | null>>({});
  let forms = $state<Record<string, Form>>({});
  let outcomes = $state<Record<string, Outcome>>({});
  let busy = $state<string | null>(null);
  let failed = $state(false);
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const live = (account: Account | null | undefined): account is Account =>
    !!account && account.revoked_at === null && account.token_fingerprint !== null;

  function askAgain(error: unknown, replay: () => Promise<void>): boolean {
    if (!needsReauth(error)) return false;
    retry = replay;
    reauthMethods = problemOf(error).reauth_methods ?? ['password'];
    return true;
  }

  async function load(): Promise<void> {
    const who = await client.get({ url: '/api/v1/me', throwOnError: false });
    if (who.error) {
      failed = true;
      return;
    }
    me = who.data as { id: string; role: string };
    if (me.role === 'child') return; // a parent enters the key of a child
    const listed = await client.get({
      url: '/api/v1/profiles/{user_id}/kavita-destinations',
      path: { user_id: me.id },
      throwOnError: false
    });
    if (listed.error) {
      failed = true;
      return;
    }
    choices = (listed.data as { items: Choice[] }).items;
    const next: Record<string, Form> = {};
    for (const choice of choices) next[choice.id] = { remote: '', key: '' };
    forms = next;
    if (!choices.some((choice) => choice.linked)) return;
    const own = await client.get({
      url: '/api/v1/profiles/{user_id}/destination-accounts',
      path: { user_id: me.id },
      throwOnError: false
    });
    if (own.error) {
      failed = true;
      return;
    }
    for (const account of (own.data as { items: Account[] }).items) {
      if (live(account)) {
        accounts[account.shelf_destination_id] = account;
        const form = forms[account.shelf_destination_id];
        if (form && account.remote_user) form.remote = account.remote_user;
      }
    }
  }

  /** Sends the key: it leaves the field before the request leaves, whatever the answer. */
  async function link(choice: Choice): Promise<void> {
    const form = forms[choice.id];
    if (!form || !me || busy) return;
    const token = form.key;
    form.key = '';
    if (token === '') return;
    await send(choice, token, form.remote.trim());
  }

  async function send(choice: Choice, token: string, remote: string): Promise<void> {
    if (!me) return;
    busy = choice.id;
    delete outcomes[choice.id];
    try {
      const { data, error } = await client.put({
        url: '/api/v1/profiles/{user_id}/destination-accounts/{shelf_destination_id}',
        path: { user_id: me.id, shelf_destination_id: choice.id },
        headers: headers(),
        body: { token, remote_user: remote === '' ? undefined : remote },
        throwOnError: false
      });
      if (error) {
        // the key is kept only inside this closure, for the replay after a fresh sign-in
        if (askAgain(error, () => send(choice, token, remote))) return;
        const reason = (problemOf(error) as { reason?: string }).reason;
        const key = `kavita.reason.${reason}`;
        outcomes[choice.id] = {
          kind: 'refused',
          problem: problemView(error),
          note: reason && hasMessage(key) ? t(key) : null
        };
        return;
      }
      const account = data as Account;
      accounts[choice.id] = account;
      outcomes[choice.id] = { kind: account.warnings?.includes('kavita_admin') ? 'admin' : 'linked' };
    } finally {
      busy = null;
    }
  }

  async function revoke(choice: Choice): Promise<void> {
    if (!me || busy) return;
    busy = choice.id;
    delete outcomes[choice.id];
    try {
      const { error } = await client.delete({
        url: '/api/v1/profiles/{user_id}/destination-accounts/{shelf_destination_id}',
        path: { user_id: me.id, shelf_destination_id: choice.id },
        headers: headers(),
        throwOnError: false
      });
      if (error) {
        if (askAgain(error, () => revoke(choice))) return;
        outcomes[choice.id] = { kind: 'refused', problem: problemView(error), note: null };
        return;
      }
      accounts[choice.id] = null;
      outcomes[choice.id] = { kind: 'revoked' };
    } finally {
      busy = null;
    }
  }

  onMount(load);
</script>

<svelte:head>
  <title>{t('kavita.title')}</title>
</svelte:head>

<main class="page">
  <div class="running-head"><span>{t('nav.settings')}</span></div>
  <div class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{t('kavita.title')}</h1>
      {#if me && me.role !== 'child' && !failed}<p class="lede">{t('kavita.intro')}</p>{/if}
    </div>
  </div>

  {#if failed}
    <div role="alert">
      <ErrorState kind="server" title={t('kavita.error_title')} headingLevel={2}>
        {t('kavita.error')}
      </ErrorState>
    </div>
  {:else if !me}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('kavita.loading')}</p>
      <Skeleton /><Skeleton short />
    </div>
  {:else if me.role === 'child'}
    <EmptyState title={t('kavita.child_title')} center>
      {t('kavita.child')}
    </EmptyState>
  {:else}
    {#if choices.length === 0}
      <EmptyState title={t('kavita.empty')} headingLevel={3} art={false} />
    {/if}
    <div class="cards">
      {#each choices as choice (choice.id)}
        {@const account = accounts[choice.id]}
        {@const form = forms[choice.id]}
        {@const outcome = outcomes[choice.id]}
        <section class="panel card" aria-labelledby="kavita-{choice.id}">
          <h2 id="kavita-{choice.id}">{t('kavita.card', { shelf: choice.shelf_name, server: choice.target_name })}</h2>
          {#if live(account)}
            <dl class="meta-list">
              <dt>{t('kavita.account', { name: account.remote_user ?? '' })}</dt>
              <dd>
                <span>{t('kavita.fingerprint')} : </span><code>{account.token_fingerprint}</code>
                {#if account.verified_at}
                  <span class="hint">({t('kavita.verified', { date: formatDateTime(account.verified_at) })})</span>
                {/if}
              </dd>
            </dl>
          {:else}
            <p class="hint">{t('kavita.none')}</p>
          {/if}
          {#if form}
            <form
              class="link"
              onsubmit={(event) => {
                event.preventDefault();
                void link(choice);
              }}
            >
              <Field label={t('kavita.remote_user')}>
                {#snippet children(control)}
                  <Input {...control} type="text" bind:value={form.remote} autocomplete="off" spellcheck={false} />
                {/snippet}
              </Field>
              <Field label={t('kavita.key')} hint={t('kavita.key_hint')}>
                {#snippet children(control)}
                  <Input {...control} type="password" bind:value={form.key} autocomplete="off" spellcheck={false} mono />
                {/snippet}
              </Field>
              <!-- not the lone last child of the form: one card per destination, so one sticky
                   action per card would stack at the bottom of a phone and cover the fields -->
              <div class="btn-row">
                <Button type="submit" variant="primary" icon="key" disabled={busy === choice.id} loading={busy === choice.id}>
                  {live(account) ? t('kavita.replace') : t('kavita.link')}
                </Button>
                {#if live(account)}
                  <Button variant="danger-ghost" icon="trash" disabled={busy === choice.id} onclick={() => revoke(choice)}>
                    {t('kavita.revoke')}
                  </Button>
                {/if}
              </div>
            </form>
          {:else if live(account)}
            <div class="btn-row">
              <Button variant="danger-ghost" icon="trash" disabled={busy === choice.id} onclick={() => revoke(choice)}>
                {t('kavita.revoke')}
              </Button>
            </div>
          {/if}
          {#if outcome?.kind === 'linked'}
            <Alert tone="success" title={t('kavita.linked')} live="polite" />
          {:else if outcome?.kind === 'admin'}
            <Alert tone="warning" title={t('kavita.linked_admin')} live="polite" />
          {:else if outcome?.kind === 'revoked'}
            <Alert tone="info" title={t('kavita.revoked')} live="polite" />
          {:else if outcome?.kind === 'refused'}
            <ProblemCard problem={outcome.problem} level={3} note={outcome.note} />
          {/if}
        </section>
      {/each}
    </div>
  {/if}

  {#if reauthMethods}
    <ReauthPrompt
      methods={reauthMethods}
      ondone={async () => {
        reauthMethods = null;
        const replay = retry;
        retry = null;
        await replay?.();
      }}
      oncancel={() => {
        reauthMethods = null;
        retry = null;
      }}
    />
  {/if}
  {#if me && me.role !== 'child'}
    <SyncStatus />
  {/if}
</main>

<style>
  .cards {
    display: grid;
    gap: var(--space-5);
  }
  .card {
    display: grid;
    gap: var(--space-3);
  }
  .card h2 {
    margin: 0;
    font-size: var(--step-1);
  }
  .meta-list {
    grid-template-columns: minmax(0, 1fr);
    gap: var(--space-1);
  }
  code {
    overflow-wrap: anywhere;
  }
  .link {
    display: grid;
    gap: var(--space-3);
    max-inline-size: var(--form-max);
  }
</style>
