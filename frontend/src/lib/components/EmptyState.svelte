<script lang="ts">
  // EF-UX-008: an empty state is never a dead end: it names what is missing and offers the
  // action that fills it.
  let {
    title,
    description = '',
    actionLabel,
    href
  }: { title: string; description?: string; actionLabel: string; href: string } = $props();
</script>

<section class="empty-state" aria-label={title}>
  <h3 class="title">{title}</h3>
  {#if description}
    <p class="description">{description}</p>
  {/if}
  <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
  <a class="action" {href}>{actionLabel}</a>
</section>

<style>
  .empty-state {
    text-align: center;
    padding: 1.5rem 1rem;
    border: 1px dashed var(--color-rule, #ccc);
    border-radius: 0.5rem;
  }
  .title {
    margin: 0 0 0.5rem;
    font-size: 1rem;
  }
  .description {
    margin: 0 0 1rem;
    color: var(--color-muted);
  }
  .action {
    display: inline-block;
    min-height: 2.75rem;
    min-width: 2.75rem;
    line-height: 2.75rem;
    padding: 0 1rem;
    border-radius: 0.5rem;
    border: 1px solid currentColor;
  }
</style>
