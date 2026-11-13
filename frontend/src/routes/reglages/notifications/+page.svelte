<script lang="ts">
  // The notification preferences of the signed-in person (L14): `GET|PUT /api/v1/me/notifications`.
  // The whole of it is replaced at once; no quiet hours go as null, never as an empty text.
  import { onMount } from 'svelte';
  import { getNotifications, putNotifications } from '#lib/api/index.ts';
  import type { NotificationPrefsOut } from '#lib/api/types.gen.ts';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';

  const MODES = ['digest', 'immediate'] as const;

  let prefs = $state<NotificationPrefsOut | null>(null);
  let notifyMode = $state<string>('digest');
  let quietStart = $state('');
  let quietEnd = $state('');
  let timezone = $state('');
  let failed = $state(false);
  let message = $state('');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });
  const orNull = (text: string): string | null => (text.trim() === '' ? null : text.trim());

  function show(data: NotificationPrefsOut): void {
    prefs = data;
    notifyMode = data.notify_mode;
    quietStart = data.quiet_start ?? '';
    quietEnd = data.quiet_end ?? '';
    timezone = data.timezone;
  }

  async function load(): Promise<void> {
    try {
      const { data } = await getNotifications({ throwOnError: true });
      show(data);
    } catch {
      failed = true;
      message = t('notifications.error');
    }
  }

  onMount(load);

  async function save(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    failed = false;
    message = '';
    try {
      const { data } = await putNotifications({
        headers: headers(),
        body: {
          notify_mode: notifyMode,
          quiet_start: orNull(quietStart),
          quiet_end: orNull(quietEnd),
          timezone
        },
        throwOnError: true
      });
      show(data);
      message = t('notifications.saved');
    } catch {
      failed = true;
      message = t('notifications.refused');
    }
  }
</script>

<svelte:head>
  <title>{t('notifications.title')}</title>
</svelte:head>

<main>
  <h1>{t('notifications.title')}</h1>
  {#if prefs}
    <form onsubmit={save}>
      <label for="notify-mode">{t('notifications.mode')}</label>
      <select id="notify-mode" bind:value={notifyMode}>
        {#each MODES as mode (mode)}
          <option value={mode}>{t(`notifications.mode.${mode}`)}</option>
        {/each}
      </select>
      <label for="quiet-start">{t('notifications.quiet_start')}</label>
      <input id="quiet-start" type="time" bind:value={quietStart} />
      <label for="quiet-end">{t('notifications.quiet_end')}</label>
      <input id="quiet-end" type="time" bind:value={quietEnd} />
      <label for="timezone">{t('notifications.timezone')}</label>
      <input id="timezone" bind:value={timezone} required />
      <button type="submit">{t('notifications.save')}</button>
    </form>
  {/if}
  {#if message}
    <p role={failed ? 'alert' : 'status'} class:err={failed}>{message}</p>
  {/if}
</main>

<style>
  main {
    max-width: 48rem;
    margin: 0 auto;
    padding: 2rem 1rem;
  }
  form {
    display: grid;
    gap: 0.5rem;
    max-width: 24rem;
  }
  .err {
    color: var(--color-err);
  }
</style>
