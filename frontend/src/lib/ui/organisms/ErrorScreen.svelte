<script lang="ts" module>
  import type { ErrorKind } from '../molecules/ErrorState.svelte';

  /** The kind of a screen that cannot be shown: no network first, then the status. */
  export function errorKind(status: number, online: boolean): ErrorKind {
    if (!online) return 'offline';
    if (status === 404) return 'not-found';
    if (status === 403) return 'forbidden';
    if (status === 401) return 'signed-out';
    return 'server';
  }
</script>

<script lang="ts">
  // The whole screen of +error.svelte, told as a library would (EF-UX-003): the missing volume
  // (404), the volume shelved higher (403, and for a child « ask a parent »), the closed session
  // (401, the sign-in page of the server), the fallen shelf (500, with the incident id), no
  // network. Never the raw message of the framework, never JSON. The shell (masthead, table of
  // contents, colophon) stays around it.
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import Button from '../atoms/Button.svelte';
  import ErrorState from '../molecules/ErrorState.svelte';
  import SearchBar from '../molecules/SearchBar.svelte';

  let {
    status,
    path = null,
    child = false,
    signedIn = false,
    online = true,
    incident = null
  }: {
    status: number;
    path?: string | null;
    child?: boolean;
    signedIn?: boolean;
    online?: boolean;
    incident?: string | null;
  } = $props();

  const kind = $derived(errorKind(status, online));
  // the words of a child are said with « tu » (the voice of the children's screens)
  const voice = $derived(child && (kind === 'forbidden' || kind === 'not-found') ? '_child' : '');
  const key = (name: string) => `error.${kind.replace('-', '_')}.${name}${voice}` as MessageKey;
  const code = $derived(kind === 'offline' ? t('error.offline.code') : t('error.code', { status }));
</script>

<svelte:head><title>{t(key('title'))} · Shelfhold</title></svelte:head>

<main class="page">
  <ErrorState {kind} title={t(key('title'))} {code} path={kind === 'not-found' ? path : null} incident={kind === 'server' ? incident : null}>
    <p>{t(key('body'))}</p>
    {#snippet actions()}
      {#if kind === 'not-found' && signedIn}
        <SearchBar action="/chercher" size="lg" label={t('error.not_found.search')} placeholder={t('ui.header.search_placeholder')} />
      {/if}
      <div class="btn-row">
        {#if kind === 'signed-out'}
          <Button href="/login" variant="primary" icon="key" data-sveltekit-reload>{t('ui.account.sign_in')}</Button>
        {:else if kind === 'forbidden' && child}
          <Button href="/chercher" variant="primary" icon="send">{t('error.forbidden.ask_parent')}</Button>
          <Button href="/" icon="home">{t('error.back_home_child')}</Button>
        {:else if kind === 'server' || kind === 'offline'}
          <Button variant="primary" icon="refresh" onclick={() => location.reload()}>{t('error.retry')}</Button>
          <Button href="/" icon="home">{t('error.back_home')}</Button>
        {:else}
          <Button href="/" variant="primary" icon="home">{child ? t('error.back_home_child') : t('error.back_home')}</Button>
          {#if signedIn && kind === 'not-found'}
            <Button href="/bibliotheque">{t('error.browse_library')}</Button>
          {/if}
        {/if}
      </div>
    {/snippet}
  </ErrorState>
</main>
