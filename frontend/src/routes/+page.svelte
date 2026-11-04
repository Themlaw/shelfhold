<script lang="ts">
  import HealthStatus from '#lib/components/HealthStatus.svelte';
  import { t } from '#lib/i18n/index.ts';
</script>

<svelte:head>
  <title>{t('home.title')}</title>
</svelte:head>

<main>
  <h1>{t('home.title')}</h1>
  <p class="tagline">{t('home.tagline')}</p>
  <HealthStatus />
</main>

<style>
  main {
    max-width: 42rem;
    margin: 0 auto;
    padding: 3rem 1rem;
  }
  h1 {
    font-family: var(--font-display);
    font-weight: 400;
    font-style: italic;
    letter-spacing: -0.02em;
    margin: 0 0 0.5rem;
  }
  .tagline {
    color: var(--color-muted);
    margin: 0 0 2rem;
  }
</style>
