<script lang="ts">
  import Footer from '#lib/components/Footer.svelte';
  import Nav from '#lib/components/Nav.svelte';
  import { onMount } from 'svelte';
  import { guardOfflineSession, startOfflineSync, wipeOfflineCopies } from '#lib/offline/runtime.ts';
  import { registerServiceWorker } from '#lib/pwa/register.ts';
  import { applyAppearance } from '#lib/themes/index.ts';
  import { detectLanguage, setUiLanguage } from '#lib/i18n/index.ts';
  import '#lib/themes/base.css';
  import '#lib/themes/themes.css';
  import '#lib/themes/eink.css';
  import './global.css';

  let { children, data } = $props();

  // The installable shell (EF-LEC-012): registered by the SPA, never by the reader, and only on a build
  // (the worker is a product of `vite build`).
  onMount(() => {
    if (import.meta.env.PROD) void registerServiceWorker(navigator, location.pathname);
  });

  // The offline copies belong to the profile signed in: another one's are wiped, what was read offline
  // is sent, what left the view is evicted (L16b). Nobody known (offline, signed out): nothing changes here.
  $effect(() => {
    // the server refused the session (revoked, expired, account off): nothing of it stays on the device
    if (data.sessionLost) void wipeOfflineCopies();
    const profile = data.profileId;
    if (!import.meta.env.PROD) return;
    // `/me` did not name anyone: another session than the one the copies were kept under is a wipe. When
    // it does, `startOfflineSync` activates that profile (wiping the others') and binds the session.
    if (!profile) {
      void guardOfflineSession(null);
      return;
    }
    return startOfflineSync(profile);
  });

  // The boot script of app.html already set the attributes from the last visit; the answer of the
  // server (another device may have changed it) applies here, once the screen has its data.
  $effect(() => {
    if (data.appearance) applyAppearance(document.documentElement, data.appearance);
    // Update the UI language when data changes (e.g., preference change or sign-in)
    // a later answer (a sign-in, a changed preference) changes the language too
    setUiLanguage(data.language ?? detectLanguage(navigator.languages));
  });
</script>

<div class="page">
  <Nav ids={data.nav ?? []} />
  <div class="screen">{@render children()}</div>
  <Footer sourceUrl={data.sourceUrl} />
</div>

<style>
  .page {
    display: flex;
    flex-direction: column;
    min-height: 100vh;
  }
  .screen {
    flex: 1;
  }
</style>
