<script lang="ts">
  // One shelf, as the sentence the admin reads (EF-UX-007). The text is the server's, shown as
  // text, never as markup.
  import Icon from '#lib/ui/atoms/Icon.svelte';

  let { shelf }: { shelf: { name: string; phrase: string } } = $props();
</script>

<p class="sentence-shelf" data-shelf={shelf.name}>
  <Icon name="books" />
  <span>{shelf.phrase}</span>
</p>

<style>
  .sentence-shelf {
    display: flex;
    align-items: flex-start;
    gap: var(--space-3);
    margin: 0;
    padding: var(--space-3) var(--space-4);
    border-left: var(--rule-thick) solid var(--color-shelf, var(--color-rule-strong));
    background: var(--color-surface);
    font: var(--step-1) / var(--leading-read) var(--font-text);
    overflow-wrap: anywhere;
  }
  .sentence-shelf :global(.icon) {
    flex: none;
    margin-top: 0.2em;
    color: var(--color-muted);
  }
</style>
