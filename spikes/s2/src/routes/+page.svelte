<script lang="ts">
  import { onMount } from 'svelte';
  import { t } from '#lib/i18n.ts';
  import { client } from '#lib/api/client.gen.ts';
  import { healthz } from '#lib/api/index.ts';
  let state = $state('…');
  onMount(async () => {
    const r = await healthz({ client });
    state = r.data?.state ?? 'erreur';
  });
</script>
<svelte:head><title>Shelfhold</title></svelte:head>
<h1>{t('title')}</h1>
<p data-testid="health">{t('health', { state })}</p>
<p>{t('count', { n: 0 })} / {t('count', { n: 1 })} / {t('count', { n: 2000 })}</p>
