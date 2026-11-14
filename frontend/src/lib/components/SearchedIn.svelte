<script lang="ts">
  // « Recherché dans… » (EF-UX-011): the sources that were asked, with what each one returned, the
  // ones that did not answer, and each one's trust card. Never an address, never an error code.
  import TrustCard from '#lib/components/TrustCard.svelte';
  import { t } from '#lib/i18n/index.ts';
  import type { SearchedInOut } from '#lib/api/types.gen.ts';

  let { sources }: { sources: SearchedInOut[] } = $props();
</script>

{#if sources && sources.length > 0}
  <section role="region" aria-label={t('searched.region')} class="searched-in">
    <h2>{t('searched.region')}</h2>
    {#each sources as source (source.source_key)}
      <article data-testid="searched-source" class="searched-source">
        <p class="name">{source.name}</p>
        {#if source.status === 'error'}
          <p class="status">{t('searched.unavailable')}</p>
        {:else if source.returned === 0}
          <p class="status">{t('searched.no_results')}</p>
        {:else}
          <p class="status">{t('searched.results', { count: source.returned })}</p>
        {/if}
        {#if source.trust}
          <TrustCard trust={source.trust} />
        {:else}
          <p>{t('trust.unavailable')}</p>
        {/if}
      </article>
    {/each}
  </section>
{/if}

<style>
  .searched-in {
    display: grid;
    gap: 0.5rem;
    margin: 1rem 0;
  }
  .searched-source {
    display: grid;
    gap: 0.25rem;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--color-rule);
  }
  .name {
    margin: 0;
    font-weight: 600;
  }
  .status {
    margin: 0;
    color: var(--color-muted);
  }
</style>
