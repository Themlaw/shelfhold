<script lang="ts">
  // A row of a table of contents: the number in italic, the title, leader dots, the end (a state,
  // a page, a date). The whole row is the link. `state`: read (greyed), current (blue ground),
  // locked (no link, italic). Put rows in an <ol class="toc"> or a TableOfContents.
  import type { Snippet } from 'svelte';

  let {
    number,
    title,
    href,
    state,
    end
  }: {
    number?: string | number | null;
    title: string;
    href?: string | null;
    state?: 'read' | 'current' | 'locked';
    end?: Snippet;
  } = $props();
</script>

<li class="chapter" class:is-read={state === 'read'} class:is-current={state === 'current'} class:is-locked={state === 'locked'}>
  <span class="chapter__num">{number ?? ''}</span>
  <span class="chapter__title">
    {#if href && state !== 'locked'}
      <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
      <a {href} aria-current={state === 'current' ? 'true' : undefined} title={title}>{title}</a>
    {:else}
      <span class="chapter__text" title={title}>{title}</span>
    {/if}
  </span>
  {#if end}<span class="chapter__end">{@render end()}</span>{/if}
</li>

<style>
  .chapter {
    position: relative;
    display: grid;
    grid-template-columns: 3rem minmax(0, 1fr) auto;
    align-items: baseline;
    gap: var(--space-3);
    min-height: var(--target);
    padding: var(--space-2);
    border-bottom: var(--rule-hair) solid var(--color-rule);
  }
  .chapter:hover {
    background: var(--color-surface);
  }
  .chapter__num {
    font: italic 600 var(--step-1) / 1 var(--font-text);
    font-variant-numeric: oldstyle-nums;
    color: var(--color-muted);
    text-align: right;
  }
  .chapter__title {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    min-width: 0;
    font: var(--step-1) / 1.3 var(--font-text);
  }
  .chapter__title a,
  .chapter__text {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--color-ink);
    text-decoration: none;
  }
  .chapter__title a::after {
    content: '';
    position: absolute;
    inset: 0;
  }
  .chapter__title a:focus-visible {
    outline: none;
  }
  .chapter:has(a:focus-visible) {
    outline: 2px solid var(--color-focus);
    outline-offset: -2px;
  }
  /* leader dots between the title and the end */
  .chapter__title::after {
    content: '';
    flex: 1 1 2rem;
    min-width: 2rem;
    border-bottom: var(--rule-mid) dotted var(--color-rule-strong);
    transform: translateY(-0.25em);
  }
  .chapter__end {
    position: relative;
    z-index: 1;
    display: flex;
    align-items: center;
    gap: var(--space-3);
    font-size: var(--step--1);
    color: var(--color-muted);
    white-space: nowrap;
  }
  .is-read .chapter__title a {
    color: var(--color-muted);
  }
  .is-current {
    background: var(--color-accent-soft);
  }
  .is-current .chapter__num {
    color: var(--color-accent-text);
  }
  .is-locked .chapter__text {
    color: var(--color-subtle);
    font-style: italic;
  }
  @media (max-width: 40rem) {
    .chapter {
      grid-template-columns: 2rem minmax(0, 1fr);
    }
    .chapter__end {
      grid-column: 2;
    }
    .chapter__title::after {
      display: none;
    }
  }
</style>
