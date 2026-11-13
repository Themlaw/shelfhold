<script lang="ts">
  import { onMount } from 'svelte';
  import { getPreferences, patchPreferences } from '#lib/api/index.ts';
  import type { PreferencesOut } from '#lib/api/types.gen.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { applyAppearance, rememberAppearance, type Appearance } from '#lib/themes/index.ts';

  let prefs = $state<PreferencesOut | null>(null);
  let theme = $state<'bibliotheque' | null>('bibliotheque');
  let colorScheme = $state<'auto' | 'light' | 'dark'>('auto');
  let failed = $state(false);
  let message = $state('');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  async function load(): Promise<void> {
    try {
      const { data } = await getPreferences({ throwOnError: true });
      prefs = data;
      theme = data.chosen_theme as 'bibliotheque' | null;
      colorScheme = data.color_scheme;
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
      const { data } = await patchPreferences({
        headers: headers(),
        body: { theme, color_scheme: colorScheme },
        throwOnError: true
      });
      prefs = data;
      const appearance: Appearance = {
        theme: data.theme as 'bibliotheque',
        colorScheme: data.color_scheme
      };
      applyAppearance(document.documentElement, appearance);
      rememberAppearance(localStorage, appearance);
      message = 'Enregistré.';
    } catch {
      failed = true;
      message = 'Action refusée.';
    }
  }
</script>

<svelte:head>
  <title>Apparence</title>
</svelte:head>

<main>
  <h1>Apparence</h1>
  <form onsubmit={save}>
    <label for="theme">Thème</label>
    <select id="theme" bind:value={theme}>
      <option value="bibliotheque">bibliotheque</option>
      <option value={null}>Par défaut</option>
    </select>
    <label for="color-scheme">Schéma de couleurs</label>
    <select id="color-scheme" bind:value={colorScheme}>
      <option value="auto">auto</option>
      <option value="light">light</option>
      <option value="dark">dark</option>
    </select>
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