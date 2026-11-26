<script lang="ts">
  // "Connect a device" for an e-reader (M4 L46b, EF-UX-004, ADR-0053 §4): pairs a reader with a
  // profile. The secret comes back once, in the answer of the creation, and lives in this
  // component's memory only: never in the address, the browser's storage or a log, and it is
  // dropped when the person says they noted it, or leaves the screen.
  import { onDestroy, onMount } from 'svelte';
  import {
    deviceList,
    deviceRemove,
    deviceRotate,
    deviceTokenPair,
    deviceTokenSetup,
    listProfiles,
    me
  } from '#lib/api/index.ts';
  import type {
    DeviceOut,
    DeviceTokenSetup,
    ProfileOut
  } from '#lib/api/types.gen.ts';
  import QrCode from '#lib/components/QrCode.svelte';
  import ReauthPrompt from '#lib/components/ReauthPrompt.svelte';
  import { Guard } from '#lib/guard.svelte.ts';
  import { formatDateTime } from '#lib/i18n/format.ts';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import ConfirmDialog from '#lib/ui/molecules/ConfirmDialog.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import ErrorState from '#lib/ui/molecules/ErrorState.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import SecretOnce from '#lib/ui/molecules/SecretOnce.svelte';
  import { notify } from '#lib/ui/toast.svelte.ts';
  import { webAddress } from '#lib/links.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { isForbidden, needsReauth, needsSession } from '#lib/triage.ts';

  let { data }: { data?: { user?: { role?: string } | null } } = $props();

  const CLOCK_SKEW_MS = 2 * 60 * 1000;

  const guard = new Guard();
  let setup = $state<DeviceTokenSetup | null>(null);
  let devices = $state<DeviceOut[]>([]);
  let profiles = $state<ProfileOut[]>([]);
  let role = $state('');
  let subject = $state('');
  let name = $state('');
  let kind = $state<DeviceOut['kind']>('other');
  let shown = $state<{ publicId: string; secret: string } | null>(null);
  let message = $state('');
  let failed = $state(false);
  let busy = $state(false);
  let loaded = $state(false);
  let asking = $state<{ device: DeviceOut; what: 'remove' | 'rotate' } | null>(null);
  let askOpen = $state(false);
  const ask = (device: DeviceOut, what: 'remove' | 'rotate') => {
    asking = { device, what };
    askOpen = true;
  };
  const labelOf = (device: DeviceOut) => device.label ?? device.public_id ?? '';
  const child = $derived(data?.user?.role === 'child');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const origin = () => (typeof window === 'undefined' ? '' : window.location.origin);
  const opdsAddress = $derived(setup ? webAddress(origin() + setup.opds_path) : null);
  const kosyncAddress = $derived(setup ? webAddress(origin() + setup.kosync_path) : null);
  const KIND_LIST: Array<[DeviceOut['kind'], MessageKey]> = [
    ['koreader', 'deviceTokens.kind_koreader'],
    ['kobo', 'deviceTokens.kind_kobo'],
    ['opds', 'deviceTokens.kind_opds'],
    ['browser', 'deviceTokens.kind_browser'],
    ['other', 'deviceTokens.kind_other']
  ];
  const kindName = (value: DeviceOut['kind']): MessageKey =>
    KIND_LIST.find(([id]) => id === value)?.[1] ?? 'deviceTokens.kind_other';
  const subjects = $derived(profiles.filter((p) => p.role !== 'admin'));
  const skewed = $derived(
    setup !== null && Math.abs(Date.now() - Date.parse(setup.server_time)) > CLOCK_SKEW_MS
  );

  async function load(): Promise<void> {
    await guard.run(async () => {
      const [me_, setup_, list] = await Promise.all([
        me({ throwOnError: true }),
        deviceTokenSetup({ throwOnError: true }),
        deviceList({ throwOnError: true })
      ]);
      role = me_.data.role;
      setup = setup_.data;
      devices = list.data.items;
      if (role === 'admin') {
        const found = await listProfiles({ throwOnError: true });
        profiles = found.data.items;
        if (!subject && subjects[0]) subject = subjects[0].id;
      }
      loaded = true;
    });
  }

  onMount(load);
  // leaving the screen takes the secret out of the page, by every way out
  onDestroy(forget);

  function forget(): void {
    shown = null;
  }

  /** An answer the screen says in place (a refused profile, a server error); a stale password, no
   * session and a refused role go to the guard, which asks again or closes the screen. */
  function said(error: unknown): void {
    if (needsReauth(error) || needsSession(error) || isForbidden(error)) throw error;
    failed = true;
    message = t('deviceTokens.error');
  }

  async function pair(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (busy) return;
    busy = true;
    failed = false;
    message = '';
    try {
      await guard.run(async () => {
        try {
          const { data } = await deviceTokenPair({
            headers: headers(),
            body: {
              name: name.trim() || null,
              kind,
              subject_user_id: role === 'admin' ? subject : null
            },
            throwOnError: true
          });
          shown = { publicId: data.public_id, secret: data.secret };
          name = '';
          const list = await deviceList({ throwOnError: true });
          devices = list.data.items;
        } catch (error) {
          said(error);
        }
      });
    } finally {
      busy = false;
    }
  }

  /** A new secret on the same device: the old one is refused at once, nothing is paired again.
   * The new one is shown once, as at pairing. */
  async function rotate(device: DeviceOut): Promise<void> {
    await guard.run(async () => {
      const { data } = await deviceRotate({
        headers: headers(),
        path: { device_id: device.id },
        throwOnError: true
      });
      shown = { publicId: data.public_id, secret: data.secret };
      notify({ title: t('deviceTokens.rotated', { name: labelOf(device) }), tone: 'info' });
    });
  }

  async function remove(device: DeviceOut): Promise<void> {
    await guard.run(async () => {
      await deviceRemove({ headers: headers(), path: { device_id: device.id }, throwOnError: true });
      if (shown?.publicId === device.public_id) forget();
      const list = await deviceList({ throwOnError: true });
      devices = list.data.items;
      notify({ title: t('deviceTokens.removed', { name: labelOf(device) }) });
    });
  }

  const MESSAGES = {
    signed_out: 'devices.signed_out',
    forbidden: 'deviceTokens.forbidden',
    failed: 'devices.error'
  } as const;
  const TITLES = {
    signed_out: 'devices.signed_out_title',
    forbidden: 'deviceTokens.forbidden_title',
    failed: 'devices.failed_title'
  } as const;
  const KINDS = { signed_out: 'signed-out', forbidden: 'forbidden', failed: 'server' } as const;

  async function copy(url: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(url);
      notify({ title: t('ui.secret.copied', { what: url }) });
    } catch {
      notify({ title: t('ui.secret.copy_failed'), tone: 'warning' });
    }
  }
</script>

<svelte:head>
  <title>{t('deviceTokens.title')}</title>
</svelte:head>

<main class="page">
  <div class="running-head">
    <span>{t('nav.devices')}</span>
  </div>
  <div class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{t('deviceTokens.title')}</h1>
      {#if guard.state === 'ok'}<p class="lede">{t('deviceTokens.lede')}</p>{/if}
    </div>
  </div>

  {#if guard.state !== 'ok'}
    <div role="alert">
    <ErrorState kind={KINDS[guard.state]} title={t(TITLES[guard.state])} headingLevel={2}>
      {guard.state === 'forbidden' && child ? t('deviceTokens.forbidden_child') : t(MESSAGES[guard.state])}
    </ErrorState>
    </div>
  {:else}
    {#if guard.reauthMethods}
      <ReauthPrompt
        methods={guard.reauthMethods}
        ondone={() => guard.afterReauth()}
        oncancel={() => guard.cancelReauth()}
      />
    {/if}

    {#if shown}
      <div class="secret">
        <SecretOnce
          title={t('deviceTokens.once_title')}
          note={t('deviceTokens.once_note')}
          fields={[
            { label: t('deviceTokens.identifier'), value: shown.publicId, testid: 'public-id' },
            { label: t('deviceTokens.secret'), value: shown.secret, secret: true, testid: 'secret' }
          ]}
          footnote={t('deviceTokens.as_is')}
          ackLabel={t('deviceTokens.noted')}
          onack={forget}
        />
      </div>
    {/if}

    {#if !loaded}
      <div aria-busy="true">
        <p class="visually-hidden" role="status">{t('deviceTokens.loading')}</p>
        <Skeleton /><Skeleton /><Skeleton short />
      </div>
    {/if}

    {#if setup}
      {#if !setup.https}
        <div class="notes">
          <Alert tone="warning" title={setup.plain_http_allowed ? t('deviceTokens.plain_allowed') : t('deviceTokens.plain_refused')} live="assertive" />
        </div>
      {/if}

      <section class="section" aria-labelledby="where">
        <div class="section-head"><h2 id="where">{t('deviceTokens.where')}</h2></div>
        <div class="cols">
          <div>
            <dl class="meta-list addresses">
              <dt>{t('deviceTokens.opds_address')}</dt>
              <dd class="address">
                <code>{opdsAddress ?? setup.opds_path}</code>
                {#if opdsAddress}
                  <Button size="sm" icon="copy" aria-label={`${t('devices.copy_address')} : ${t('deviceTokens.opds_address')}`} onclick={() => copy(opdsAddress)}>{t('ui.secret.copy_short')}</Button>
                {/if}
              </dd>
              <dt>{t('deviceTokens.kosync_address')}</dt>
              <dd class="address">
                <code>{kosyncAddress ?? setup.kosync_path}</code>
                {#if kosyncAddress}
                  <Button size="sm" icon="copy" aria-label={`${t('devices.copy_address')} : ${t('deviceTokens.kosync_address')}`} onclick={() => copy(kosyncAddress)}>{t('ui.secret.copy_short')}</Button>
                {/if}
              </dd>
            </dl>
            <p>{t('deviceTokens.https_required')}</p>
            <p class="hint">
              {t('deviceTokens.server_time', { time: formatDateTime(setup.server_time) })}
            </p>
            {#if skewed}
              <Alert tone="warning" title={t('deviceTokens.clock_skew')} live="polite" />
            {/if}
            <p class="hint">{t('deviceTokens.clock_note')}</p>
          </div>
          {#if opdsAddress}
            <figure class="qr">
              <QrCode text={opdsAddress} label={t('deviceTokens.qr_label', { url: opdsAddress })} />
              <figcaption class="hint">{t('deviceTokens.qr_note')}</figcaption>
            </figure>
          {/if}
        </div>
        <h3 class="technical">{t('deviceTokens.technical')}</h3>
        <ul class="technical-list">
          <li>{t('deviceTokens.method_binary')}</li>
          <li>{t('deviceTokens.single_server')}</li>
          <li>{t('deviceTokens.in_clear')}</li>
        </ul>
      </section>
    {/if}

    {#if loaded && (role === 'admin' || role === 'member')}
      <section class="section" aria-labelledby="new-title">
        <div class="section-head"><h2 id="new-title">{t('deviceTokens.new')}</h2></div>
        <form class="new" onsubmit={pair} aria-labelledby="new-title">
          {#if role === 'admin'}
            <Field label={t('deviceTokens.profile')} hint={t('deviceTokens.admin_note')} required>
              {#snippet children(control)}
                <Select {...control} bind:value={subject} options={subjects.map((p) => ({ value: p.id, label: p.username }))} />
              {/snippet}
            </Field>
          {/if}
          <Field label={t('deviceTokens.kind')}>
            {#snippet children(control)}
              <Select {...control} bind:value={kind} options={KIND_LIST.map(([id, label]) => ({ value: id, label: t(label) }))} />
            {/snippet}
          </Field>
          <Field label={t('deviceTokens.name')}>
            {#snippet children(control)}
              <Input {...control} type="text" bind:value={name} maxlength={80} autocomplete="off" />
            {/snippet}
          </Field>
          <div class="btn-row">
            <Button type="submit" variant="primary" icon="plus" disabled={role === 'admin' && !subject} loading={busy}>
              {t('deviceTokens.create')}
            </Button>
          </div>
        </form>
        {#if failed && message}
          <div class="notes"><Alert tone="danger" title={message} live="assertive" /></div>
        {/if}
      </section>
    {/if}

    {#if loaded}
      <section class="section" aria-labelledby="list-title">
        <div class="section-head"><h2 id="list-title">{t('deviceTokens.list')}</h2></div>
        {#if devices.length === 0}
          <EmptyState title={t('deviceTokens.none')} headingLevel={3} art={false} />
        {:else}
          <ul class="entries" role="list">
            {#each devices as device (device.id)}
              <li class="device">
                <div class="device__text">
                  <p class="device__name">{labelOf(device)}</p>
                  <p class="device__meta">
                    {t(kindName(device.kind))} · {device.subject_username}
                    <span class="hint">
                      {device.last_seen_at
                        ? t('deviceTokens.last_seen', { time: formatDateTime(device.last_seen_at) })
                        : t('deviceTokens.never_seen')}
                    </span>
                  </p>
                </div>
                <div class="btn-row">
                  <Button size="sm" icon="refresh" onclick={() => ask(device, 'rotate')}>{t('deviceTokens.rotate')}</Button>
                  <Button size="sm" variant="danger-ghost" icon="trash" onclick={() => ask(device, 'remove')}>{t('deviceTokens.remove')}</Button>
                </div>
              </li>
            {/each}
          </ul>
        {/if}
        <p class="hint">{t('deviceTokens.revoke_note')}</p>
      </section>
    {/if}
  {/if}
</main>

{#if asking}
  <ConfirmDialog
    bind:open={askOpen}
    title={asking.what === 'remove'
      ? t('deviceTokens.remove_title', { name: labelOf(asking.device) })
      : t('deviceTokens.rotate_title', { name: labelOf(asking.device) })}
    message={asking.what === 'remove' ? t('deviceTokens.remove_message') : t('deviceTokens.rotate_message')}
    confirmLabel={asking.what === 'remove' ? t('deviceTokens.remove') : t('deviceTokens.rotate_confirm')}
    tone={asking.what === 'remove' ? 'danger' : 'primary'}
    onconfirm={async () => {
      const current = asking;
      if (!current) return;
      if (current.what === 'remove') await remove(current.device);
      else await rotate(current.device);
    }}
  />
{/if}

<style>
  .secret,
  .notes {
    margin-bottom: var(--space-5);
  }
  .addresses {
    grid-template-columns: minmax(0, 1fr);
    gap: var(--space-1);
    margin-bottom: var(--space-4);
  }
  .addresses dd {
    margin-bottom: var(--space-3);
  }
  .address {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2) var(--space-3);
  }
  .address code {
    flex: 1 1 14rem;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .qr {
    margin: 0;
    display: grid;
    gap: var(--space-2);
    justify-items: start;
  }
  .technical {
    margin-top: var(--space-5);
    font-size: var(--step-0);
    font-family: var(--font-ui);
  }
  .technical-list {
    margin: 0;
    padding-left: 1.2em;
    color: var(--color-muted);
    max-width: var(--measure);
  }
  .new {
    display: grid;
    gap: var(--space-4);
    max-inline-size: var(--form-max);
  }
  .device {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2) var(--space-4);
    padding-block: var(--space-3);
  }
  .device p {
    margin: 0;
  }
  .device__name {
    font: 600 var(--step-1) / var(--leading-tight) var(--font-text);
    overflow-wrap: anywhere;
  }
  .device__meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1) var(--space-3);
    color: var(--color-muted);
  }
</style>
