<script lang="ts">
  import Footer from '#lib/components/Footer.svelte';
  import { applyAppearance } from '#lib/themes/index.ts';
  import '#lib/themes/base.css';
  import '#lib/themes/themes.css';
  import './global.css';

  let { children, data } = $props();

  // The boot script of app.html already set the attributes from the last visit; the answer of the
  // server (another device may have changed it) applies here, once the screen has its data.
  $effect(() => {
    if (data.appearance) applyAppearance(document.documentElement, data.appearance);
  });
</script>

<div class="page">
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
