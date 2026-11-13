<script lang="ts">
  // ENF-LEG-012 (AGPL section 13): the link to the source of the running version, under every
  // screen. A scheme other than http(s) is never turned into a link.
  import { t } from '#lib/i18n/index.ts';

  let { sourceUrl }: { sourceUrl: string | null } = $props();

  const safe = $derived(sourceUrl !== null && /^https?:\/\/[^\s]+$/i.test(sourceUrl) ? sourceUrl : null);
</script>

<footer>
  {#if safe}
    <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- an external address -->
    <a href={safe} rel="noopener noreferrer">{t('footer.source')}</a>
  {:else}
    <span>{t('footer.source_unknown')}</span>
  {/if}
</footer>

<style>
  footer {
    max-width: 42rem;
    margin: 0 auto;
    padding: 1.5rem 1rem 2rem;
    border-top: 1px solid var(--color-rule);
    color: var(--color-muted);
    font-size: 0.875rem;
  }
  a {
    color: var(--color-accent);
    min-height: 2.75rem;
    min-width: 2.75rem;
    display: inline-flex;
    align-items: center;
  }
</style>
