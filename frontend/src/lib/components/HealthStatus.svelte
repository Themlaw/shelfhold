<script lang="ts">
  import { onMount } from 'svelte';
  import { healthz } from '#lib/api/index.ts';
  import { t } from '#lib/i18n/index.ts';

  let message = $state(t('health.loading'));
  let failed = $state(false);

  onMount(async () => {
    try {
      const { data } = await healthz({ throwOnError: true });
      if (data.state === 'migrating') {
        message = t('health.migrating', {
          done: data.progress?.done ?? 0,
          total: data.progress?.total ?? 0
        });
      } else {
        message = t('health.ok');
      }
    } catch {
      failed = true;
      message = t('health.error');
    }
  });
</script>

<section aria-labelledby="health-heading">
  <h2 id="health-heading">{t('health.heading')}</h2>
  {#if failed}
    <p role="alert" class="status err">{message}</p>
  {:else}
    <p role="status" class="status">{message}</p>
  {/if}
</section>

<style>
  .status {
    margin: 0;
    color: var(--color-muted);
  }
  .err {
    color: var(--color-err);
  }
</style>
