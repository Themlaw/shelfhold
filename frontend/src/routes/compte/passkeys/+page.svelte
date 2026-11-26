<script lang="ts">
  import { onMount } from 'svelte';
  import {
    passkeyRegister,
    passkeyRegistrationOptions,
    passkeyRevoke,
    passkeysList
  } from '#lib/api/index.ts';
  import type { PasskeyItem } from '#lib/api/types.gen.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { formatDate, t } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import ConfirmDialog from '#lib/ui/molecules/ConfirmDialog.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import { createPasskey, csrfToken, passkeysSupported } from '#lib/passkeys.ts';
  import { needsReauth, problemOf } from '#lib/triage.ts';

  let items = $state<PasskeyItem[]>([]);
  let available = $state(false);
  let reason = $state<string | null>(null);
  let name = $state('');
  let message = $state('');
  let failed = $state(false);
  let busy = $state(false);
  let loaded = $state(false);
  let revoking = $state<PasskeyItem | null>(null);
  let revokeOpen = $state(false);
  let reauthMethods = $state<string[] | null>(null);
  let retry: (() => Promise<void>) | null = null;

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  async function load(): Promise<void> {
    try {
      const { data } = await passkeysList({ throwOnError: true });
      items = data.items;
      available = data.available && passkeysSupported();
      reason = data.available ? t('passkeys.unsupported') : (data.reason ?? null);
    } catch {
      failed = true;
      message = t('passkeys.error');
    }
    loaded = true;
  }

  onMount(load);

  /** A stale password asks for it in place (EF-USR-022), then runs the same action again. */
  function askReauth(error: unknown, again: () => Promise<void>): boolean {
    if (!needsReauth(error)) return false;
    retry = again;
    reauthMethods = problemOf(error).reauth_methods ?? ['password'];
    failed = false;
    message = '';
    return true;
  }

  async function afterReauth(): Promise<void> {
    const again = retry;
    retry = null;
    reauthMethods = null;
    if (again) await again();
  }

  function cancelReauth(): void {
    retry = null;
    reauthMethods = null;
  }

  function register(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    return enrol();
  }

  async function enrol(): Promise<void> {
    busy = true;
    failed = false;
    message = '';
    try {
      const { data: started } = await passkeyRegistrationOptions({
        headers: headers(),
        throwOnError: true
      });
      const credential = await createPasskey(started.options);
      await passkeyRegister({
        headers: headers(),
        body: { flow: started.flow, name: name.trim(), credential },
        throwOnError: true
      });
      name = '';
      message = t('passkeys.added');
      await load();
    } catch (error) {
      if (!askReauth(error, enrol)) {
        failed = true;
        message = t('passkeys.error');
      }
    } finally {
      busy = false;
    }
  }

  async function revoke(item: PasskeyItem): Promise<void> {
    failed = false;
    message = '';
    try {
      await passkeyRevoke({
        headers: headers(),
        path: { passkey_id: item.id },
        throwOnError: true
      });
      message = t('passkeys.revoked');
      await load();
    } catch (error) {
      if (askReauth(error, () => revoke(item))) return;
      failed = true;
      const problem = (error ?? {}) as { code?: string };
      message = problem.code === 'last_login_factor' ? t('passkeys.last_factor') : t('passkeys.error');
    }
  }
</script>

<svelte:head>
  <title>{t('passkeys.title')}</title>
</svelte:head>

<main class="page page--form">
  <div class="running-head"><span>{t('nav.settings')}</span></div>
  <div class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{t('passkeys.title')}</h1>
      <p class="lede">{t('passkeys.lede')}</p>
    </div>
  </div>

  {#if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('passkeys.loading')}</p>
      <Skeleton /><Skeleton short />
    </div>
  {:else}
    <section class="section" aria-labelledby="pk-list">
      <div class="section-head"><h2 id="pk-list">{t('passkeys.list')}</h2></div>
      {#if items.length === 0}
        <EmptyState title={t('passkeys.none')} headingLevel={3} art={false} />
      {:else}
        <ul class="entries" role="list">
          {#each items as item (item.id)}
            <li class="key">
              <div class="key__text">
                <p class="key__name">{item.name}</p>
                <p class="key__meta hint">
                  {t('passkeys.created', { date: formatDate(item.created_at, { day: 'numeric', month: 'long', year: 'numeric' }) })}
                  · {item.last_used_at
                    ? t('passkeys.last_used', { date: formatDate(item.last_used_at, { day: 'numeric', month: 'long', year: 'numeric' }) })
                    : t('passkeys.never_used')}
                </p>
              </div>
              <Button size="sm" variant="danger-ghost" icon="trash" onclick={() => { revoking = item; revokeOpen = true; }}>
                {t('passkeys.revoke')}
              </Button>
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <section class="section" aria-labelledby="pk-new">
      <div class="section-head"><h2 id="pk-new">{t('passkeys.new')}</h2></div>
      {#if available}
        <form class="new" onsubmit={register}>
          <Field label={t('passkeys.name')} hint={t('passkeys.name_help')} required>
            {#snippet children(control)}
              <Input {...control} bind:value={name} maxlength={64} autocomplete="off" />
            {/snippet}
          </Field>
          <div class="btn-row">
            <Button type="submit" variant="primary" icon="key" loading={busy}>{t('passkeys.add')}</Button>
          </div>
        </form>
      {:else if reason}
        <Alert tone="info" title={t('passkeys.unavailable_title')}>
          <p>{reason}</p>
        </Alert>
      {/if}
    </section>
  {/if}

  {#if reauthMethods}
    <ReauthPrompt methods={reauthMethods} ondone={afterReauth} oncancel={cancelReauth} />
  {/if}

  {#if message}
    <div class="notice">
      <Alert tone={failed ? 'danger' : 'success'} title={message} live={failed ? 'assertive' : 'polite'} />
    </div>
  {/if}
</main>

{#if revoking}
  <ConfirmDialog
    bind:open={revokeOpen}
    title={t('passkeys.revoke_title', { name: revoking.name })}
    message={t('passkeys.revoke_message')}
    confirmLabel={t('passkeys.revoke')}
    onconfirm={async () => {
      const target = revoking;
      if (target) await revoke(target);
    }}
  />
{/if}

<style>
  .key {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2) var(--space-4);
    padding-block: var(--space-3);
  }
  .key p {
    margin: 0;
  }
  .key__name {
    font: 600 var(--step-1) / var(--leading-tight) var(--font-text);
    overflow-wrap: anywhere;
  }
  .new {
    display: grid;
    gap: var(--space-4);
  }
  .notice {
    margin-top: var(--space-5);
  }
</style>
