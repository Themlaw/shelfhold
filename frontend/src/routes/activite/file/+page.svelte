<script lang="ts">
  // The admin's queue and maintenance screen (EF-INT-017, EF-INT-018). The role is checked by the
  // server on every call; the component shows its refusal and nothing else.
  import QueueControls from '#lib/components/QueueControls.svelte';
  import { t } from '#lib/i18n/index.ts';
</script>

<svelte:head>
  <title>{t('queue.title')}</title>
</svelte:head>

<main>
  <h1>{t('queue.title')}</h1>
  <QueueControls />
</main>

<style>
  main {
    max-width: 48rem;
    margin: 0 auto;
    padding: 1rem;
  }
</style>
