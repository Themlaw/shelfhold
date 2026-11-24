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

  type Shown = { hook: WebhookOut; deliveries: DeliveryOut[]; unreadable: boolean };
  type Secret = { value: string; kind: 'created' | 'rotated'; hookId: string };

  const STATES: Record<string, MessageKey> = {
    delivered: 'webhooks.state.delivered',
    pending: 'webhooks.state.pending',
    dead: 'webhooks.state.dead'
  };

  let hooks = $state<Shown[]>([]);
  let adminOnly = $state(false);
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

<main class="form-screen">
  <h1>{t('webhooks.title')}</h1>

  {#if adminOnly}
    <p role="alert">{notice?.text}</p>
  {:else}
    <form
      onsubmit={(event) => {
        event.preventDefault();
        void create();
      }}
    >
      <label for="webhook-url">{t('webhooks.url')}</label>
      <input id="webhook-url" type="url" bind:value={url} required />
      <label for="webhook-types">{t('webhooks.types')}</label>
      <input id="webhook-types" bind:value={types} required />
      <label>
        <input type="checkbox" bind:checked={allowPrivate} />
        {t('webhooks.allow_private')}
      </label>
      <button type="submit" disabled={busy}>{t('webhooks.add')}</button>
    </form>

    {#if secret}
      <section aria-labelledby="secret-title">
        <h2 id="secret-title">{t('webhooks.secret.title')}</h2>
        <p>{t(secret.kind === 'created' ? 'webhooks.secret.created' : 'webhooks.secret.rotated')}</p>
        <code>{secret.value}</code>
        <p>{t('webhooks.secret.once')}</p>
      </section>
    {/if}

    {#if hooks.length === 0}
      <p>{t('webhooks.empty')}</p>
    {/if}

    <ul>
      {#each hooks as shown (shown.hook.id)}
        <li>
          <span>{shown.hook.url_host}</span>
          <span>{shown.hook.event_types.join(', ')}</span>
          {#if shown.hook.private_target}
            <span>{t('webhooks.private', { endpoint: shown.hook.private_target })}</span>
          {/if}
          <button type="button" disabled={busy} onclick={() => rotate(shown.hook)}>
            {t('webhooks.rotate')}
          </button>
          <button type="button" disabled={busy} onclick={() => remove(shown.hook)}>
            {t('webhooks.delete')}
          </button>

          {#if shown.unreadable}
            <p>{t('webhooks.deliveries.unreadable')}</p>
          {:else if shown.deliveries.length === 0}
            <p>{t('webhooks.deliveries.empty')}</p>
          {:else}
            <table>
              <caption>{t('webhooks.deliveries')}</caption>
              <thead>
                <tr>
                  <th scope="col">{t('webhooks.deliveries.event')}</th>
                  <th scope="col">{t('webhooks.deliveries.state')}</th>
                  <th scope="col">{t('webhooks.deliveries.attempts')}</th>
                  <th scope="col">{t('webhooks.deliveries.status')}</th>
                  <th scope="col">{t('webhooks.deliveries.error')}</th>
                  <th scope="col">{t('webhooks.deliveries.delivered_at')}</th>
                  <th scope="col">{t('webhooks.deliveries.next_try')}</th>
                </tr>
              </thead>
              <tbody>
                {#each shown.deliveries as delivery (delivery.id)}
                  <tr>
                    <th scope="row">{delivery.event_id}</th>
                    <td>{stateText(delivery.status)}</td>
                    <td>{delivery.attempts}</td>
                    <td>{delivery.last_status ?? t('webhooks.dash')}</td>
                    <td>{delivery.last_error ?? t('webhooks.dash')}</td>
                    <td>{when(delivery.delivered_at)}</td>
                    <td>{delivery.status === 'pending' ? when(delivery.next_try_at) : t('webhooks.dash')}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          {/if}
        </li>
      {/each}
    </ul>

    {#if notice}
      <p role={notice.kind}>{notice.text}</p>
    {/if}
  {/if}

  {#if reauth.reauthMethods}
    <ReauthPrompt
      methods={reauth.reauthMethods}
      ondone={() => reauth.afterReauth()}
      oncancel={() => reauth.cancelReauth()}
    />
  {/if}
</main>

<style>
  main {
    max-width: 60rem;
    margin: 0 auto;
    padding: 2rem 1rem;
  }
  form {
    display: grid;
    gap: 0.5rem;
    max-width: 32rem;
    margin-bottom: 1.5rem;
  }
  ul {
    list-style: none;
    padding: 0;
  }
  li {
    margin-bottom: 1.5rem;
  }
  section {
    margin: 1rem 0;
    padding: 1rem;
    border: 1px solid var(--color-warn);
    border-radius: var(--radius-cover);
  }
  table {
    margin-top: 0.5rem;
    border-collapse: collapse;
  }
  caption {
    text-align: left;
    font-weight: 600;
    padding-bottom: 0.25rem;
  }
  th,
  td {
    padding: 0.25rem 0.75rem 0.25rem 0;
    text-align: left;
  }
</style>
