<script lang="ts">
  // « Lire hors ligne » (M5 L16b, EF-LEC-012): keeps the publication on this device, for the profile
  // signed in, and says so. The copy is the profile's: it goes at sign-out, when the profile changes,
  // and when the work leaves the profile's view (the server says so at the next connection).
  import { t } from '#lib/i18n/index.ts';
  import { keepCopy, OfflineKeepError } from '#lib/offline/keep.ts';
  import { offlineApi, offlineStore } from '#lib/offline/runtime.ts';

  let { unitId }: { unitId: string | null } = $props();
  let state = $state<'idle' | 'keeping' | 'kept' | 'failed' | 'excerpt' | 'not_readable' | 'no_profile'>('idle');

  async function current(): Promise<{ profile: string; kept: boolean } | null> {
    if (!unitId) return null;
    try {
      const store = offlineStore();
      const profile = await store.activeProfile();
      return profile ? { profile, kept: (await store.find(profile, 'unit', unitId)) !== undefined } : null;
    } catch {
      return null; // no storage in this browser: nothing can be kept
    }
  }

  $effect(() => {
    void unitId;
    state = 'idle';
    void current().then((found) => {
      if (found?.kept) state = 'kept';
    });
  });

  async function keep(): Promise<void> {
    if (!unitId) return;
    state = 'keeping';
    const found = await current();
    if (!found) {
      state = 'no_profile';
      return;
    }
    try {
      await keepCopy({ store: offlineStore(), profileId: found.profile, api: offlineApi(), carrierType: 'unit', carrierId: unitId });
      state = 'kept';
      // the page of the reader and its files are kept too, so that the book opens with no network
      void navigator.serviceWorker?.ready.then((registration) => registration.active?.postMessage({ type: 'shelfhold-warm-reader' }));
    } catch (error) {
      state = error instanceof OfflineKeepError && (error.reason === 'excerpt' || error.reason === 'not_readable') ? error.reason : 'failed';
    }
  }

  async function remove(): Promise<void> {
    const found = unitId ? await current() : null;
    if (!found || !unitId) return;
    const copy = await offlineStore().find(found.profile, 'unit', unitId);
    if (copy) await offlineStore().evictCarrier(found.profile, copy.carrierType, copy.carrierId);
    state = 'idle';
  }
</script>

{#if unitId}
  {#if state === 'kept'}
    <p class="kept" role="status">{t('offline.kept')}</p>
    <button type="button" onclick={remove}>{t('offline.remove')}</button>
    <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
    <a href="/hors-ligne">{t('offline.library')}</a>
  {:else}
    <button type="button" disabled={state === 'keeping'} onclick={keep}>
      {state === 'keeping' ? t('offline.keeping') : t('offline.keep')}
    </button>
  {/if}
  {#if state === 'failed' || state === 'excerpt' || state === 'not_readable' || state === 'no_profile'}
    <p role="alert" class="err">
      {t(state === 'failed' ? 'offline.failed' : state === 'excerpt' ? 'offline.excerpt_only' : state === 'not_readable' ? 'offline.not_readable' : 'offline.no_profile')}
    </p>
  {/if}
{/if}

<style>
  .kept {
    margin: 0;
  }
  button {
    min-height: 2.75rem;
    min-width: 2.75rem;
  }
</style>
