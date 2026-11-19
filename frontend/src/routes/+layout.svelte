<script lang="ts">
  import Footer from '#lib/components/Footer.svelte';
  import Nav from '#lib/components/Nav.svelte';
  import { onMount } from 'svelte';
  import { registerServiceWorker } from '#lib/pwa/register.ts';
  import { applyAppearance } from '#lib/themes/index.ts';
  import { detectLanguage, setUiLanguage } from '#lib/i18n/index.ts';
  import '#lib/themes/base.css';
  import '#lib/themes/themes.css';
  import './global.css';

  let { children, data } = $props();

  // The installable shell (EF-LEC-012): registered by the SPA, never by the reader, and only on a build
  // (the worker is a product of `vite build`).
  onMount(() => {
    if (import.meta.env.PROD) void registerServiceWorker(navigator, location.pathname);
  });

  // The boot script of app.html already set the attributes from the last visit; the answer of the
  // server (another device may have changed it) applies here, once the screen has its data.
  $effect(() => {
    if (data.appearance) applyAppearance(document.documentElement, data.appearance);
    // the language of the interface: the profile preference, else the browser languages
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
