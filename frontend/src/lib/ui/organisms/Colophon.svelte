<script lang="ts">
  // The colophon at the end of every screen, the way a book ends: a fleuron, when the page was
  // printed, what it is set in, and the links that stay true everywhere: the reader-light
  // version, and the source of the running version (ENF-LEG-012, AGPL section 13). A scheme
  // other than http(s) is never turned into a link.
  import { formatDate, t } from '#lib/i18n/index.ts';
  import { FLEURON } from '../atoms/icons.ts';
  import Shapes from '../atoms/Shapes.svelte';

  let {
    sourceUrl,
    printedAt = new Date(),
    liteHref = '/lite'
  }: { sourceUrl: string | null; printedAt?: Date; liteHref?: string | null } = $props();

  const safe = $derived(sourceUrl !== null && /^https?:\/\/[^\s]+$/i.test(sourceUrl) ? sourceUrl : null);
  const day = $derived(formatDate(printedAt, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }));
  const time = $derived(formatDate(printedAt, { hour: 'numeric', minute: '2-digit' }));
</script>

<footer class="colophon">
  <svg class="colophon__fleuron" viewBox="0 0 40 16" aria-hidden="true" focusable="false"><Shapes shapes={FLEURON} /></svg>
  <p class="colophon__line">{t('ui.colophon.printed', { day, time })}</p>
  <p class="colophon__line">{t('ui.colophon.set_in')}</p>
  <p class="colophon__links">
    {#if liteHref}
      <!-- the reader-light pages belong to the server: a full load -->
      <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- a page of the server -->
      <a href={liteHref} data-sveltekit-reload>{t('ui.colophon.lite')}</a>
    {/if}
    {#if safe}
      <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- an external address -->
      <a href={safe} rel="noopener noreferrer">{t('footer.source')}</a>
    {:else}
      <span>{t('footer.source_unknown')}</span>
    {/if}
  </p>
</footer>

<style>
  .colophon {
    margin-top: var(--space-6);
    padding: var(--space-6) var(--space-5);
    font: italic var(--step--1) / 1.6 var(--font-text);
    color: var(--color-muted);
    text-align: center;
  }
  .colophon__fleuron {
    display: block;
    width: 2.5rem;
    height: 1rem;
    margin: 0 auto var(--space-3);
    fill: none;
    stroke: currentColor;
    stroke-width: 1.3;
  }
  .colophon__line {
    margin: 0;
  }
  .colophon__line + .colophon__line {
    margin-top: var(--space-1);
  }
  .colophon__links {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 0 var(--space-4);
    margin: var(--space-2) 0 0;
    font-family: var(--font-ui);
    font-style: normal;
  }
  .colophon__links > * {
    display: inline-flex;
    align-items: center;
    min-height: var(--target);
  }
  .colophon a {
    color: var(--color-muted);
  }
</style>
