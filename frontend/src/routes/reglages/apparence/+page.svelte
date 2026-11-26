<script lang="ts">
  // The theme, the color scheme and the language of the signed-in person (EF-UX-012, ENF-I18N-001): saved by
  // the server, then the root layout reads the preferences again and applies them (only it knows
  // the theme mechanism, I51).
  import { invalidateAll } from '$app/navigation';
  import { onMount } from 'svelte';
  import { getPreferences, patchPreferences } from '#lib/api/index.ts';
  import type { PreferencesOut } from '#lib/api/types.gen.ts';
  import { hasMessage, setUiLanguage, t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

  let prefs = $state<PreferencesOut | null>(null);
  const SCHEMES: PreferencesOut['color_scheme'][] = ['auto', 'light', 'dark'];
  // the options speak their own language: the choice of one is readable whatever the interface says
  const LANGUAGES: PreferencesOut['language'][] = ['fr', 'en'];

  let theme = $state('');
  let colorScheme = $state<PreferencesOut['color_scheme']>('auto');
  let language = $state<PreferencesOut['language']>('fr');
  let failed = $state(false);
  let message = $state('');

  // a theme the server delivers before this screen knows its name shows under its own name
  const themeLabel = (name: string): string => {
    const key = `appearance.theme.${name}`;
    return hasMessage(key) ? t(key) : name;
  };

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  async function load(): Promise<void> {
    try {
      const { data } = await getPreferences({ throwOnError: true });
      prefs = data;
      theme = data.theme;
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

  // The themes are the ones the server delivers; the choice applies at once, then the layout reads
  // the preference again and sets it on the page (only it knows the mechanism, I51). A refusal puts
  // the select back on what is kept.
  async function changeTheme(): Promise<void> {
    failed = false;
    message = '';
    try {
      const { data } = await patchPreferences({
        headers: headers(),
        body: { theme },
        throwOnError: true
      });
      prefs = data;
      theme = data.theme;
      message = t('appearance.saved');
      await invalidateAll();
    } catch {
      failed = true;
      message = t('appearance.refused');
      theme = prefs?.theme ?? theme;
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

<main class="page">
  <PageHead
    title={t('appearance.title')}
    crumbs={[{ label: t('settings.title'), href: '/reglages' }, { label: t('appearance.title') }]}
  />
  {#if prefs}
    <div class="stack-lg page--form">
      <form onsubmit={(event) => event.preventDefault()}>
        <Field label={t('appearance.theme')}>
          {#snippet children(control)}
            <Select
              {...control}
              bind:value={theme}
              onchange={changeTheme}
              options={prefs!.available_themes.map((name) => ({ value: name, label: themeLabel(name) }))}
            />
          {/snippet}
        </Field>
      </form>
      <form class="stack" onsubmit={save}>
        <Field label={t('appearance.scheme')}>
          {#snippet children(control)}
            <Select
              {...control}
              bind:value={colorScheme}
              options={SCHEMES.map((scheme) => ({ value: scheme, label: t(`appearance.scheme.${scheme}`) }))}
            />
          {/snippet}
        </Field>
        <div class="btn-row">
          <Button type="submit" variant="primary">{t('appearance.save')}</Button>
        </div>
      </form>
      <form onsubmit={(event) => event.preventDefault()}>
        <Field label={t('appearance.language')}>
          {#snippet children(control)}
            <Select
              {...control}
              bind:value={language}
              onchange={changeLanguage}
              options={LANGUAGES.map((value) => ({ value, label: value === 'fr' ? 'Français' : 'English' }))}
            />
          {/snippet}
        </Field>
      </form>
    </div>
  {:else if !failed}
    <div aria-busy="true"><Skeleton /><Skeleton /><Skeleton short /></div>
  {/if}
  {#if message}
    <div class="notice">
      <Alert
        tone={failed ? 'danger' : 'success'}
        live={failed ? 'assertive' : 'polite'}
        title={message}
      />
    </div>
  {/if}
</main>

<style>
  .notice {
    max-width: var(--form-max);
    margin-top: var(--space-4);
  }
</style>
