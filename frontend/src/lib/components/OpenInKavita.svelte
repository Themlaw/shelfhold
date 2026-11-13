<script lang="ts">
  // EF-UX-010: the link that opens the book where it is read. The address is the server's; a value
  // that is not a web address gives no link.
  import { webAddress } from '#lib/links.ts';
  import { t } from '#lib/i18n/index.ts';

  let { url }: { url: string | null | undefined } = $props();
  const href = $derived(webAddress(url));
</script>

{#if href}
  <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the address of another application -->
  <a class="open-in-kavita" {href} rel="noopener noreferrer">{t('activity.open_in_kavita')}</a>
{/if}

<style>
  .open-in-kavita {
    color: var(--color-accent);
    text-decoration: underline;
    min-height: 2.75rem;
    display: inline-flex;
    align-items: center;
  }
</style>
