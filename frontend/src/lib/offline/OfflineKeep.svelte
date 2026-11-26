<script lang="ts">
  // « Lire hors ligne » (M5 L16b, EF-LEC-012; M6 L20, D-P7 (b)): keeps the publication on this
  // device, for the profile signed in, and says so. One control for both places it lives — the
  // reader, which every profile opens, and the sheet of a series, which is the administrator's.
  // The copy is the profile's: it goes at sign-out, when the profile changes, and when the work
  // leaves the profile's view (the server says so at the next connection).
  import { t } from '#lib/i18n/index.ts';
  import { keepCopy, OfflineKeepError } from '#lib/offline/keep.ts';
  import { offlineApi, offlineStore } from '#lib/offline/runtime.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import IconButton from '#lib/ui/atoms/IconButton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';

  // `compact`: icon buttons, for a toolbar (the reader); otherwise buttons with their words (the
  // sheet of a series).
  let {
    carrierType,
    carrierId,
    compact = false
  }: { carrierType: string; carrierId: string | null; compact?: boolean } = $props();
  let state = $state<'idle' | 'keeping' | 'kept' | 'failed' | 'excerpt' | 'not_readable' | 'no_profile'>('idle');

  async function current(): Promise<{ profile: string; kept: boolean } | null> {
    if (!carrierId) return null;
    try {
      const store = offlineStore();
      const profile = await store.activeProfile();
      const token = csrfToken();
      if (token && (await store.sessionMatches(token)) === false) return null; // another profile's session
      return profile ? { profile, kept: (await store.find(profile, carrierType, carrierId)) !== undefined } : null;
    } catch {
      return null; // no storage in this browser: nothing can be kept
    }
  }

  $effect(() => {
    void carrierId;
    state = 'idle';
    void current().then((found) => {
      if (found?.kept) state = 'kept';
    });
  });

  async function keep(): Promise<void> {
    if (!carrierId) return;
    state = 'keeping';
    const found = await current();
    if (!found) {
      state = 'no_profile';
      return;
    }
    try {
      await keepCopy({ store: offlineStore(), profileId: found.profile, api: offlineApi(), carrierType, carrierId });
      state = 'kept';
      // the page of the reader and its files are kept too, so that the book opens with no network
      void navigator.serviceWorker?.ready.then((registration) => registration.active?.postMessage({ type: 'shelfhold-warm-reader' }));
    } catch (error) {
      state = error instanceof OfflineKeepError && (error.reason === 'excerpt' || error.reason === 'not_readable') ? error.reason : 'failed';
    }
  }

  async function remove(): Promise<void> {
    const found = carrierId ? await current() : null;
    if (!found || !carrierId) return;
    const copy = await offlineStore().find(found.profile, carrierType, carrierId);
    if (copy) await offlineStore().evictCarrier(found.profile, copy.carrierType, copy.carrierId);
    state = 'idle';
  }
</script>

{#if carrierId}
  {#if state === 'kept'}
    <span class="kept" role="status"><Badge tone="success" icon="offline">{t('offline.kept')}</Badge></span>
    {#if compact}
      <IconButton icon="trash" label={t('offline.remove')} onclick={remove} />
    {:else}
      <Button variant="ghost" icon="trash" onclick={remove}>{t('offline.remove')}</Button>
    {/if}
    <Button href="/hors-ligne" variant="ghost" size="sm">{t('offline.library')}</Button>
  {:else if compact}
    <IconButton
      icon="download"
      label={state === 'keeping' ? t('offline.keeping') : t('offline.keep')}
      disabled={state === 'keeping'}
      onclick={keep}
    />
  {:else}
    <Button icon="download" loading={state === 'keeping'} disabled={state === 'keeping'} onclick={keep}>
      {state === 'keeping' ? t('offline.keeping') : t('offline.keep')}
    </Button>
  {/if}
  {#if state === 'failed' || state === 'excerpt' || state === 'not_readable' || state === 'no_profile'}
    <div class="note">
      <Alert
        tone={state === 'failed' ? 'danger' : 'warning'}
        live="assertive"
        title={t(state === 'failed' ? 'offline.failed' : state === 'excerpt' ? 'offline.excerpt_only' : state === 'not_readable' ? 'offline.not_readable' : 'offline.no_profile')}
      />
    </div>
  {/if}
{/if}

<style>
  .kept {
    display: inline-flex;
  }
  /* in a bar that wraps, the message takes a row of its own */
  .note {
    flex: 1 1 100%;
    min-width: 0;
  }
</style>
