<script lang="ts">
  // The webhooks screen (EF-INT-005): list, create, rotate, delete, and the last deliveries of each.
  // The server is the gate: creating, rotating and deleting are the reserved action
  // `webhook.manage` (a fresh session of an administrator, never a key), so the fresh-session
  // prompt may come first and the same call is replayed; reading the list and the deliveries is
  // the administrator's alone, and a refusal says so instead of showing an empty list.
  //
  // The secret of a creation or of a rotation is shown once, in the answer, and nowhere else: it
  // is kept in this component's state only, replaced by the next one, and never put back in the
  // list. What is shown of the address is its host (its path is often a token).
  import { onMount } from 'svelte';
  import {
    createWebhook,
    listDeliveries,
    listWebhooks,
    removeWebhook,
    rotateWebhook
  } from '#lib/api/index.ts';
  import type { DeliveryOut, WebhookOut } from '#lib/api/types.gen.ts';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import { formatDateTime } from '#lib/i18n/format.ts';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { csrfToken } from '#lib/passkeys.ts';
  import { Reauth } from '#lib/guard.svelte.ts';
  import { isForbidden, problemOf } from '#lib/triage.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Checkbox from '#lib/ui/atoms/Checkbox.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import ConfirmDialog from '#lib/ui/molecules/ConfirmDialog.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import DataTable from '#lib/ui/organisms/DataTable.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

  type Shown = { hook: WebhookOut; deliveries: DeliveryOut[]; unreadable: boolean };
  type Secret = { value: string; kind: 'created' | 'rotated'; hookId: string };

  const STATES: Record<string, MessageKey> = {
    delivered: 'webhooks.state.delivered',
    pending: 'webhooks.state.pending',
    dead: 'webhooks.state.dead'
  };

  let hooks = $state<Shown[]>([]);
  let adminOnly = $state(false);
  let loaded = $state(false);
  let deleting = $state<WebhookOut | null>(null);
  let notice = $state<{ text: string; kind: 'status' | 'alert' } | null>(null);
  let secret = $state<Secret | null>(null);
  let busy = $state(false);
  const reauth = new Reauth();

  let url = $state('');
  let types = $state('');
  let allowPrivate = $state(false);

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const stateText = (status: string): string =>
    STATES[status] ? t(STATES[status]) : t('webhooks.state.unknown');
  const when = (value: string | null): string => (value ? formatDateTime(value) : t('webhooks.dash'));

  const DELIVERY_COLUMNS = [
    { key: 'event', label: t('webhooks.deliveries.event'), primary: true },
    { key: 'state', label: t('webhooks.deliveries.state') },
    { key: 'attempts', label: t('webhooks.deliveries.attempts'), num: true },
    { key: 'status', label: t('webhooks.deliveries.status'), num: true },
    { key: 'error', label: t('webhooks.deliveries.error') },
    { key: 'delivered', label: t('webhooks.deliveries.delivered_at') },
    { key: 'next', label: t('webhooks.deliveries.next_try') }
  ] as const;
  const STATE_TONE: Record<string, 'success' | 'warning' | 'danger' | 'neutral'> = {
    delivered: 'success',
    pending: 'warning',
    dead: 'danger'
  };
  const deliveryRows = (shown: Shown) =>
    shown.deliveries.map((delivery) => ({
      id: delivery.id,
      event: delivery.event_id,
      state: delivery.status,
      attempts: delivery.attempts,
      status: delivery.last_status ?? t('webhooks.dash'),
      error: delivery.last_error ?? t('webhooks.dash'),
      delivered: when(delivery.delivered_at),
      next: delivery.status === 'pending' ? when(delivery.next_try_at) : t('webhooks.dash')
    }));

  function say(text: string, kind: 'status' | 'alert'): void {
    notice = { text, kind };
  }

  /** The deliveries of one subscription; a read that fails leaves the list standing. */
  async function deliveriesOf(hook: WebhookOut): Promise<Shown> {
    const { data, error } = await listDeliveries({
      path: { webhook_id: hook.id },
      throwOnError: false
    });
    if (error) {
      const problem = problemOf(error);
      // 404 means the subscription was deleted, keep it with empty deliveries
      if (problem.code === 'webhook.not_found') {
        return { hook, deliveries: [], unreadable: false };
      }
      // For secret_unreadable, we can still show the hook but mark deliveries as unreadable
      return { hook, deliveries: [], unreadable: true };
    }
    if (!data) return { hook, deliveries: [], unreadable: true };
    return { hook, deliveries: data.items, unreadable: false };
  }

  async function load(): Promise<void> {
    const { data, error } = await listWebhooks({ throwOnError: false });
    loaded = true;
    if (error) {
      const problem = problemOf(error);
      if (isForbidden(error)) {
        adminOnly = true;
        say(t('screens.admin_only'), 'alert');
      } else if (problem.code === 'webhook.not_found') {
        adminOnly = false;
        say(t('webhooks.not_found'), 'alert');
      } else {
        adminOnly = false;
        say(t('webhooks.error'), 'alert');
      }
      return;
    }
    adminOnly = false;
    hooks = await Promise.all(data.items.map(deliveriesOf));
  }

  /** The types are typed as one text: commas or spaces separate them, blanks are dropped. */
  const eventTypes = (): string[] =>
    types
      .split(/[\s,]+/)
      .map((piece) => piece.trim())
      .filter((piece) => piece !== '');

  async function create(): Promise<void> {
    if (busy) return;
    busy = true;
    try {
      const { data, error } = await createWebhook({
        headers: headers(),
        body: { url, event_types: eventTypes(), allow_private: allowPrivate },
        throwOnError: false
      });
      if (error) {
        if (reauth.askAgain(error, create)) return;
        const code = problemOf(error).code;
        say(
          t(
            code === 'webhook.event_types'
              ? 'webhooks.event_types_refused'
              : code === 'webhook.url_refused'
                ? 'webhooks.url_refused'
                : 'webhooks.refused'
          ),
          'alert'
        );
        return;
      }
      secret = { value: data.secret, kind: 'created', hookId: data.id };
      url = '';
      types = '';
      allowPrivate = false;
      say(t('webhooks.created'), 'status');
      await load();
    } finally {
      busy = false;
    }
  }

  async function rotate(hook: WebhookOut): Promise<void> {
    if (busy) return;
    busy = true;
    try {
      const { data, error } = await rotateWebhook({
        path: { webhook_id: hook.id },
        headers: headers(),
        throwOnError: false
      });
      if (error) {
        if (reauth.askAgain(error, () => rotate(hook))) return;
        const problem = problemOf(error);
        say(
          problem.code === 'webhook.secret_unreadable'
            ? t('webhooks.secret_unreadable')
            : t('webhooks.rotate_refused'),
          'alert'
        );
        return;
      }
      secret = { value: data.secret, kind: 'rotated', hookId: hook.id };
      say(t('webhooks.rotated'), 'status');
    } finally {
      busy = false;
    }
  }

  async function remove(hook: WebhookOut): Promise<void> {
    if (busy) return;
    busy = true;
    try {
      const { error } = await removeWebhook({
        path: { webhook_id: hook.id },
        headers: headers(),
        throwOnError: false
      });
      if (error) {
        if (reauth.askAgain(error, () => remove(hook))) return;
        say(t('webhooks.delete_refused'), 'alert');
        return;
      }
      if (secret !== null && secret.hookId === hook.id) secret = null; // only clear if it's the same subscription
      say(t('webhooks.deleted'), 'status');
      await load();
    } finally {
      busy = false;
    }
  }

  onMount(load);
</script>

<svelte:head>
  <title>{t('webhooks.title')}</title>
</svelte:head>

<main class="page">
  <PageHead
    title={t('webhooks.title')}
    crumbs={[{ label: t('settings.title'), href: '/reglages' }, { label: t('webhooks.title') }]}
    lede={t('webhooks.lede')}
  />

  {#if adminOnly}
    <Alert tone="danger" title={notice?.text ?? ''} live="assertive" />
  {:else if !loaded}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('webhooks.loading')}</p>
      <Skeleton shape="block" /><Skeleton /><Skeleton short />
    </div>
  {:else}
    <form
      class="panel add"
      aria-label={t('webhooks.add')}
      onsubmit={(event) => {
        event.preventDefault();
        void create();
      }}
    >
      <Field label={t('webhooks.url')} required>
        {#snippet children(control)}
          <Input {...control} type="url" bind:value={url} />
        {/snippet}
      </Field>
      <Field label={t('webhooks.types')} required hint={t('webhooks.types_hint')}>
        {#snippet children(control)}
          <Input {...control} mono bind:value={types} />
        {/snippet}
      </Field>
      <Checkbox bind:checked={allowPrivate} label={t('webhooks.allow_private')} />
      <div><Button type="submit" variant="primary" icon="plus" loading={busy}>{t('webhooks.add')}</Button></div>
    </form>

    {#if secret}
      <Alert tone="warning" title={t('webhooks.secret.title')} live="polite">
        <p>{t(secret.kind === 'created' ? 'webhooks.secret.created' : 'webhooks.secret.rotated')}</p>
        <code class="secret">{secret.value}</code>
        <p><strong>{t('webhooks.secret.once')}</strong></p>
      </Alert>
    {/if}

    {#if notice}
      <Alert tone={notice.kind === 'alert' ? 'danger' : 'success'} live={notice.kind === 'alert' ? 'assertive' : 'polite'} title={notice.text} />
    {/if}

    {#if hooks.length === 0}
      <EmptyState title={t('webhooks.empty')} />
    {/if}

    <ul class="hooks">
      {#each hooks as shown (shown.hook.id)}
        <li class="hook">
          <div class="hook__head">
            <div class="hook__id">
              <span class="hook__host">{shown.hook.url_host}</span>
              <span class="hook__types">{shown.hook.event_types.join(', ')}</span>
              {#if shown.hook.private_target}
                <Badge tone="warning" icon="alert">{t('webhooks.private', { endpoint: shown.hook.private_target })}</Badge>
              {/if}
            </div>
            <div class="btn-row">
              <Button size="sm" icon="refresh" disabled={busy} onclick={() => rotate(shown.hook)}>
                {t('webhooks.rotate')}
              </Button>
              <Button size="sm" variant="danger-ghost" icon="trash" disabled={busy} onclick={() => (deleting = shown.hook)}>
                {t('webhooks.delete')}
              </Button>
            </div>
          </div>

          {#if shown.unreadable}
            <Alert tone="warning" title={t('webhooks.deliveries.unreadable')} />
          {:else if shown.deliveries.length === 0}
            <p class="muted">{t('webhooks.deliveries.empty')}</p>
          {:else}
            <DataTable caption={t('webhooks.deliveries')} compact columns={DELIVERY_COLUMNS} rows={deliveryRows(shown)} key={(row) => String(row.id)}>
              {#snippet cell(column, row)}
                {#if column.key === 'state'}
                  <Badge tone={STATE_TONE[String(row.state)] ?? 'neutral'}>{stateText(String(row.state))}</Badge>
                {:else}
                  {String((row as Record<string, unknown>)[column.key] ?? '')}
                {/if}
              {/snippet}
            </DataTable>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}

  {#if reauth.reauthMethods}
    <ReauthPrompt
      methods={reauth.reauthMethods}
      ondone={() => reauth.afterReauth()}
      oncancel={() => reauth.cancelReauth()}
    />
  {/if}
</main>

<ConfirmDialog
  bind:open={() => deleting !== null, (value) => { if (!value) deleting = null; }}
  title={t('webhooks.delete_title', { host: deleting?.url_host ?? '' })}
  message={t('webhooks.delete_message')}
  confirmLabel={t('webhooks.delete_confirm')}
  onconfirm={async () => {
    const hook = deleting;
    deleting = null;
    if (hook) await remove(hook);
  }}
/>

<style>
  .add {
    display: grid;
    gap: var(--space-4);
    max-width: var(--form-max);
    margin-bottom: var(--space-6);
  }
  .secret {
    display: block;
    margin: var(--space-2) 0;
    padding: var(--space-2) var(--space-3);
    background: var(--color-surface-2);
    border-radius: var(--radius-sm);
    font-family: var(--font-mono, ui-monospace, monospace);
    overflow-wrap: anywhere;
    user-select: all;
  }
  .hooks {
    list-style: none;
    margin: var(--space-6) 0 0;
    padding: 0;
    display: grid;
    gap: var(--space-6);
  }
  .hook {
    display: grid;
    gap: var(--space-3);
  }
  .hook__head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    padding-bottom: var(--space-2);
    border-bottom: var(--rule-hair) solid var(--color-ink);
  }
  .hook__id {
    display: grid;
    gap: var(--space-1);
    min-width: 0;
  }
  .hook__host {
    font: 500 var(--step-1) / var(--leading-tight) var(--font-text);
    overflow-wrap: anywhere;
  }
  .hook__types {
    color: var(--color-muted);
    font-size: var(--step--1);
    overflow-wrap: anywhere;
  }
</style>
