<script lang="ts">
  // The color scheme and the language of the signed-in person (EF-UX-012, ENF-I18N-001): saved by
  // the server, then the root layout reads the preferences again and applies them (only it knows
  // the theme mechanism, I51).
  import { invalidateAll } from '$app/navigation';
  import { onMount } from 'svelte';
  import { getPreferences, patchPreferences } from '#lib/api/index.ts';
  import type { PreferencesOut } from '#lib/api/types.gen.ts';
  import { setUiLanguage, t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';

  let prefs = $state<PreferencesOut | null>(null);
  const SCHEMES: PreferencesOut['color_scheme'][] = ['auto', 'light', 'dark'];
  // the options speak their own language: the choice of one is readable whatever the interface says
  const LANGUAGES: PreferencesOut['language'][] = ['fr', 'en'];

  let colorScheme = $state<PreferencesOut['color_scheme']>('auto');
  let language = $state<PreferencesOut['language']>('fr');
  let failed = $state(false);
  let message = $state('');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  async function load(): Promise<void> {
    try {
      const { data } = await getPreferences({ throwOnError: true });
      prefs = data;
      colorScheme = data.color_scheme;
      language = data.language;
      setUiLanguage(data.language);
    } catch {
      failed = true;
      message = t('appearance.error');
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
        body: { color_scheme: colorScheme },
        throwOnError: true
      });
      prefs = data;
      message = t('appearance.saved');
      await invalidateAll();
    } catch {
      failed = true;
      message = t('appearance.refused');
    }
  }

  // The language applies at once (the screen re-renders in it), then the layout reads the
  // preference again like it does for the theme.
  async function changeLanguage(): Promise<void> {
    failed = false;
    message = '';
    setUiLanguage(language);
    try {
      const { data } = await patchPreferences({
        headers: headers(),
        body: { language },
        throwOnError: true
      });
      prefs = data;
      message = t('appearance.saved');
      await invalidateAll();
    } catch {
      failed = true;
      message = t('appearance.refused');
    }
  }
</script>

<svelte:head>
  <title>{t('appearance.title')}</title>
</svelte:head>

<main>
  <h1>{t('appearance.title')}</h1>
  {#if prefs}
    <form onsubmit={save}>
      <label for="color-scheme">{t('appearance.scheme')}</label>
      <select id="color-scheme" bind:value={colorScheme}>
        {#each SCHEMES as scheme (scheme)}
          <option value={scheme}>{t(`appearance.scheme.${scheme}`)}</option>
        {/each}
      </select>
      <button type="submit">{t('appearance.save')}</button>
    </form>
    <form onsubmit={(event) => event.preventDefault()}>
      <label for="language">{t('appearance.language')}</label>
      <select id="language" bind:value={language} onchange={changeLanguage}>
        {#each LANGUAGES as value (value)}
          <option value={value}>{value === 'fr' ? 'Français' : 'English'}</option>
        {/each}
      </select>
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
