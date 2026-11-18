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
  import { webAddress } from '#lib/links.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { isForbidden, needsReauth, needsSession } from '#lib/triage.ts';

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

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const origin = () => (typeof window === 'undefined' ? '' : window.location.origin);
  const opdsAddress = $derived(setup ? webAddress(origin() + setup.opds_path) : null);
  const kosyncAddress = $derived(setup ? webAddress(origin() + setup.kosync_path) : null);
  const KINDS: Array<[DeviceOut['kind'], MessageKey]> = [
    ['koreader', 'deviceTokens.kind_koreader'],
    ['kobo', 'deviceTokens.kind_kobo'],
    ['opds', 'deviceTokens.kind_opds'],
    ['browser', 'deviceTokens.kind_browser'],
    ['other', 'deviceTokens.kind_other']
  ];
  const kindName = (value: DeviceOut['kind']): MessageKey =>
    KINDS.find(([id]) => id === value)?.[1] ?? 'deviceTokens.kind_other';
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
      message = t('deviceTokens.rotated', { name: device.label ?? device.public_id ?? '' });
    });
  }

  async function remove(device: DeviceOut): Promise<void> {
    await guard.run(async () => {
      await deviceRemove({ headers: headers(), path: { device_id: device.id }, throwOnError: true });
      if (shown?.publicId === device.public_id) forget();
      const list = await deviceList({ throwOnError: true });
      devices = list.data.items;
      message = t('deviceTokens.removed', { name: device.label ?? device.public_id ?? '' });
    });
  }

  const MESSAGES = {
    signed_out: 'devices.signed_out',
    forbidden: 'deviceTokens.forbidden',
    failed: 'devices.error'
  } as const;
</script>

<svelte:head>
  <title>{t('deviceTokens.title')}</title>
</svelte:head>

<main>
  <h1>{t('deviceTokens.title')}</h1>

  {#if guard.state !== 'ok'}
    <p role="alert" class="err">{t(MESSAGES[guard.state])}</p>
  {:else}
    {#if guard.reauthMethods}
      <ReauthPrompt
        methods={guard.reauthMethods}
        ondone={() => guard.afterReauth()}
        oncancel={() => guard.cancelReauth()}
      />
    {/if}

    <p>{t('deviceTokens.intro')}</p>

    {#if setup}
      {#if !setup.https}
        <p role="alert" class="err">
          {setup.plain_http_allowed
            ? t('deviceTokens.plain_allowed')
            : t('deviceTokens.plain_refused')}
        </p>
      {/if}

      <section aria-labelledby="where">
        <h2 id="where">{t('deviceTokens.where')}</h2>
        <dl>
          <dt>{t('deviceTokens.opds_address')}</dt>
          <dd><code>{opdsAddress ?? setup.opds_path}</code></dd>
          <dt>{t('deviceTokens.kosync_address')}</dt>
          <dd><code>{kosyncAddress ?? setup.kosync_path}</code></dd>
        </dl>
        {#if opdsAddress}
          <QrCode
            text={opdsAddress}
            label={t('deviceTokens.qr_label', { url: opdsAddress })}
          />
          <p class="muted">{t('deviceTokens.qr_note')}</p>
        {/if}
        <p>{t('deviceTokens.https_required')}</p>
        <p>
          {t('deviceTokens.server_time', {
            time: formatDateTime(setup.server_time)
          })}
        </p>
        {#if skewed}
          <p role="status" class="err">{t('deviceTokens.clock_skew')}</p>
        {/if}
        <p>{t('deviceTokens.clock_note')}</p>
        <ul>
          <li>{t('deviceTokens.method_binary')}</li>
          <li>{t('deviceTokens.single_server')}</li>
          <li>{t('deviceTokens.in_clear')}</li>
        </ul>
      </section>
    {/if}

    {#if shown}
      <section class="once" aria-labelledby="once-title">
        <h2 id="once-title">{t('deviceTokens.once_title')}</h2>
        <p>{t('deviceTokens.once_note')}</p>
        <p>
          {t('deviceTokens.identifier')} : <code data-testid="public-id">{shown.publicId}</code>
        </p>
        <p>
          {t('deviceTokens.secret')} :
          <code class="secret" data-testid="secret">{shown.secret}</code>
        </p>
        <p class="muted">{t('deviceTokens.as_is')}</p>
        <button type="button" onclick={forget}>{t('deviceTokens.noted')}</button>
      </section>
    {/if}

    {#if role === 'admin' || role === 'member'}
      <form onsubmit={pair} aria-label={t('deviceTokens.new')}>
        <h2>{t('deviceTokens.new')}</h2>
        {#if role === 'admin'}
          <label>
            {t('deviceTokens.profile')}
            <select bind:value={subject} required>
              {#each subjects as profile (profile.id)}
                <option value={profile.id}>{profile.username}</option>
              {/each}
            </select>
          </label>
          <p class="muted">{t('deviceTokens.admin_note')}</p>
        {/if}
        <label>
          {t('deviceTokens.kind')}
          <select bind:value={kind}>
            {#each KINDS as [id, label] (id)}
              <option value={id}>{t(label)}</option>
            {/each}
          </select>
        </label>
        <label>
          {t('deviceTokens.name')}
          <input type="text" bind:value={name} maxlength="80" autocomplete="off" />
        </label>
        <button type="submit" disabled={busy || (role === 'admin' && !subject)}>
          {t('deviceTokens.create')}
        </button>
      </form>
    {/if}

    {#if message}
      <p role="status" class:err={failed}>{message}</p>
    {/if}

    <section aria-labelledby="list-title">
      <h2 id="list-title">{t('deviceTokens.list')}</h2>
      {#if devices.length === 0}
        <p>{t('deviceTokens.none')}</p>
      {:else}
        <ul class="tokens">
          {#each devices as device (device.id)}
            <li>
              <span>
                <strong>{device.label ?? device.public_id ?? ''}</strong>
                · {t(kindName(device.kind))} · {device.subject_username}
                <br />
                <span class="muted">
                  {device.last_seen_at
                    ? t('deviceTokens.last_seen', { time: formatDateTime(device.last_seen_at) })
                    : t('deviceTokens.never_seen')}
                </span>
              </span>
              <span class="actions">
                <button type="button" onclick={() => rotate(device)}>
                  {t('deviceTokens.rotate')}
                </button>
                <button type="button" onclick={() => remove(device)}>
                  {t('deviceTokens.remove')}
                </button>
              </span>
            </li>
          {/each}
        </ul>
      {/if}
      <p class="muted">{t('deviceTokens.revoke_note')}</p>
    </section>
  {/if}
</main>

<style>
  main {
    max-width: 48rem;
    margin: 0 auto;
    padding: 3rem 1rem;
  }
  h1 {
    font-family: var(--font-display);
    font-weight: 400;
    font-style: italic;
    letter-spacing: -0.02em;
  }
  section,
  form {
    margin: 1.5rem 0;
    display: grid;
    gap: 0.5rem;
  }
  label {
    display: grid;
    gap: 0.25rem;
  }
  .once {
    padding: 1rem;
    border: 1px solid var(--color-rule);
    border-radius: var(--radius-cover);
    background: var(--color-surface);
  }
  .secret {
    font-size: 1.25em;
    letter-spacing: 0.05em;
    user-select: all;
  }
  .tokens {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 0.5rem;
  }
  .actions {
    display: flex;
    gap: 0.5rem;
  }
  .tokens li {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    align-items: center;
  }
  code,
  p,
  dd {
    overflow-wrap: anywhere;
  }
  .muted {
    color: var(--color-muted);
  }
  .err {
    color: var(--color-err);
  }
</style>
