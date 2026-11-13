<script lang="ts">
  import { onMount } from 'svelte';
  import { getNotifications, putNotifications } from '#lib/api/index.ts';
  import type { NotificationPrefsOut } from '#lib/api/types.gen.ts';
  import { csrfToken } from '#lib/passkeys.ts';

  let prefs = $state<NotificationPrefsOut | null>(null);
  let notifyMode = $state('digest');
  let quietStart = $state<string | null>(null);
  let quietEnd = $state<string | null>(null);
  let timezone = $state('UTC');
  let failed = $state(false);
  let message = $state('');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  async function load(): Promise<void> {
    try {
      const { data } = await getNotifications({ throwOnError: true });
      prefs = data;
      notifyMode = data.notify_mode;
      quietStart = data.quiet_start;
      quietEnd = data.quiet_end;
      timezone = data.timezone;
    } catch {
      failed = true;
      message = 'Erreur de chargement.';
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
          quiet_start: quietStart,
          quiet_end: quietEnd,
          timezone
        },
        throwOnError: true
      });
      prefs = data;
      message = 'Enregistré.';
    } catch {
      failed = true;
      message = 'Action refusée.';
    }
  }
</script>

<svelte:head>
  <title>Notifications</title>
</svelte:head>

<main>
  <h1>Notifications</h1>
  <form onsubmit={save}>
    <label for="notify-mode">Mode</label>
    <select id="notify-mode" bind:value={notifyMode}>
      <option value="digest">digest</option>
      <option value="immediate">immediate</option>
    </select>
    <label for="quiet-start">Début de quiétude</label>
    <input id="quiet-start" bind:value={quietStart} />
    <label for="quiet-end">Fin de quiétude</label>
    <input id="quiet-end" bind:value={quietEnd} />
    <label for="timezone">Fuseau</label>
    <input id="timezone" bind:value={timezone} required />
    <button type="submit">Enregistrer</button>
  </form>
  {#if message}
    <p role={failed ? 'alert' : 'status'} class:err={failed}>{message}</p>
  {/if}
</main>

<style>
  main { max-width: 48rem; margin: 0 auto; padding: 2rem 1rem; }
  form { display: grid; gap: 0.5rem; max-width: 24rem; }
  .err { color: var(--color-err, #b00); }
</style>