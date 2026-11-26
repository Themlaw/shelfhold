<script lang="ts">
  // « Recherché dans… » (EF-UX-011): the sources that were asked, with what each one returned, the
  // ones that did not answer, and each one's trust card. Never an address, never an error code.
  import TrustCard from '#lib/components/TrustCard.svelte';
  import { t } from '#lib/i18n/index.ts';
  import type { SearchedInOut } from '#lib/api/types.gen.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';

  let { sources }: { sources: SearchedInOut[] } = $props();
</script>

{#if sources && sources.length > 0}
  <section aria-label={t('searched.region')} class="section searched-in">
    <div class="section-head">
      <h2>{t('searched.region')}</h2>
    </div>
    <div class="searched-list">
      {#each sources as source (source.source_key)}
        <article data-testid="searched-source" class="searched-source" class:searched-source--down={source.status === 'error'}>
          <div class="head">
            <p class="name">{source.name}</p>
            {#if source.status === 'error'}
              <Badge tone="warning">{t('searched.unavailable')}</Badge>
            {:else if source.returned === 0}
              <Badge>{t('searched.no_results')}</Badge>
            {:else}
              <Badge tone="success">{t('searched.results', { count: source.returned })}</Badge>
            {/if}
          </div>
          {#if source.trust}
            <TrustCard trust={source.trust} />
          {:else}
            <p class="hint">{t('trust.unavailable')}</p>
          {/if}
        </article>
      {/each}
    </div>
  </section>
{/if}

<style>
  .searched-list {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 18rem), 1fr));
    gap: var(--space-4);
  }
  .searched-source {
    display: grid;
    align-content: start;
    gap: var(--space-3);
    min-width: 0;
    padding: var(--space-4);
    background: var(--color-surface);
    border: var(--rule-hair) solid var(--color-rule);
    border-radius: var(--radius-md);
  }
  .searched-source--down {
    border-color: var(--color-warn);
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2) var(--space-3);
  }
  .name {
    margin: 0;
    font-weight: 700;
    overflow-wrap: anywhere;
  }
  .hint {
    margin: 0;
  }
</style>
