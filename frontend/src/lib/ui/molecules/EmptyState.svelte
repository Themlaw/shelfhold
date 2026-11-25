<script lang="ts">
  // An empty place is never a dead end: one sentence says what the place is for, and one action
  // says what to do next. `variant="filtered"` is the empty answer to a filter: it recalls the
  // filter and offers to clear it, with another drawing than the first use.
  import type { Snippet } from 'svelte';
  import Illustration from '../atoms/Illustration.svelte';

  let {
    title,
    variant = 'first',
    headingLevel = 2,
    center = false,
    art = true,
    children,
    actions
  }: {
    title: string;
    variant?: 'first' | 'filtered';
    headingLevel?: 2 | 3;
    center?: boolean;
    art?: boolean;
    children?: Snippet;
    actions?: Snippet;
  } = $props();
</script>

<div class="empty empty--{variant}" class:empty--center={center}>
  {#if art}
    <div class="empty__art"><Illustration name={variant === 'filtered' ? 'nothing-found' : 'empty'} /></div>
  {/if}
  <svelte:element this={`h${headingLevel}`} class="empty__title">{title}</svelte:element>
  {#if children}<div class="empty__body">{@render children()}</div>{/if}
  {#if actions}<div class="empty__actions">{@render actions()}</div>{/if}
</div>

<style>
  .empty {
    display: grid;
    justify-items: start;
    gap: var(--space-3);
    max-width: 36rem;
    padding: var(--space-6) 0;
  }
  .empty--center {
    justify-items: center;
    text-align: center;
    margin-inline: auto;
  }
  .empty__art {
    width: 9rem;
    color: var(--color-muted);
  }
  .empty__title {
    margin: 0;
    font-size: var(--step-2);
    font-weight: 500;
  }
  .empty__body {
    color: var(--color-muted);
    font: var(--step-1) / var(--leading-read) var(--font-text);
  }
  .empty__body :global(p) {
    margin: 0;
  }
  .empty__actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-3);
  }
</style>
