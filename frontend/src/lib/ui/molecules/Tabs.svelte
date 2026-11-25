<script lang="ts" module>
  export type TabItem = {
    value: string;
    label: string;
    /** the label on a phone, when the full one is long (« Exports » for « Exports et liseuses ») */
    short?: string;
    count?: number;
    /** link tabs: each tab is a page of its own (aria-current), no panel */
    href?: string;
    disabled?: boolean;
  };
</script>

<script lang="ts">
  // Tabs. With panels (bits-ui: arrows move between tabs, Home and End too, each tab owns its
  // panel); or, when every item has `href`, a navigation between pages that look like tabs, the
  // current one marked by aria-current. The row scrolls sideways on a phone, and a shadow at
  // the edge says there is more.
  import type { Snippet } from 'svelte';
  import { Tabs } from 'bits-ui';
  import { formatNumber } from '#lib/i18n/index.ts';

  let {
    items,
    value = $bindable(items[0]?.value ?? ''),
    label,
    panel
  }: {
    items: readonly TabItem[];
    value?: string;
    label: string;
    /** the content of the panel of a tab (panel mode) */
    panel?: Snippet<[TabItem]>;
  } = $props();

  const links = $derived(items.length > 0 && items.every((item) => item.href));
</script>

{#snippet text(item: TabItem)}
  {#if item.short}
    <span class="tab__long">{item.label}</span><span class="tab__short" aria-hidden="true">{item.short}</span>
  {:else}
    {item.label}
  {/if}
  {#if item.count !== undefined}<span class="tab__count">{formatNumber(item.count)}</span>{/if}
{/snippet}

{#if links}
  <nav class="tabs" aria-label={label}>
    {#each items as item (item.value)}
      <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
      <a class="tab" href={item.href} aria-current={item.value === value ? 'page' : undefined}>{@render text(item)}</a>
    {/each}
  </nav>
{:else}
  <Tabs.Root bind:value>
    <Tabs.List>
      {#snippet child({ props })}
        <div {...props} class="tabs" aria-label={label}>
          {#each items as item (item.value)}
            <Tabs.Trigger value={item.value} disabled={item.disabled}>
              {#snippet child({ props: tabProps })}
                <button {...tabProps} class="tab" type="button">{@render text(item)}</button>
              {/snippet}
            </Tabs.Trigger>
          {/each}
        </div>
      {/snippet}
    </Tabs.List>
    {#each items as item (item.value)}
      <Tabs.Content value={item.value}>
        {#snippet child({ props })}
          <div {...props} class="tab-panel">{@render panel?.(item)}</div>
        {/snippet}
      </Tabs.Content>
    {/each}
  </Tabs.Root>
{/if}

<style>
  .tabs {
    display: flex;
    gap: var(--space-5);
    border-bottom: var(--rule-hair) solid var(--color-rule-strong);
    overflow-x: auto;
    scrollbar-width: none;
    scroll-snap-type: x proximity;
    /* the shadow at an edge that hides more tabs */
    background:
      linear-gradient(to right, var(--color-bg) 45%, transparent) left / 2.5rem 100% no-repeat local,
      linear-gradient(to left, var(--color-bg) 45%, transparent) right / 2.5rem 100% no-repeat local,
      linear-gradient(to right, color-mix(in oklab, var(--color-ink) 30%, transparent), transparent) left / 0.75rem 100% no-repeat scroll,
      linear-gradient(to left, color-mix(in oklab, var(--color-ink) 30%, transparent), transparent) right / 0.75rem 100% no-repeat scroll;
  }
  .tab {
    position: relative;
    flex: none;
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    min-height: var(--target);
    padding: 0 0 var(--space-1);
    font: 500 var(--step-0) / 1 var(--font-ui);
    color: var(--color-muted);
    text-decoration: none;
    white-space: nowrap;
    background: none;
    border: 0;
    cursor: pointer;
    scroll-snap-align: start;
  }
  .tab:hover {
    color: var(--color-ink);
  }
  .tab[aria-selected='true'],
  .tab[aria-current='page'] {
    color: var(--color-ink);
    font-weight: 700;
  }
  .tab[aria-selected='true']::after,
  .tab[aria-current='page']::after {
    content: '';
    position: absolute;
    left: 0;
    right: 0;
    bottom: calc(-1 * var(--rule-hair));
    border-bottom: var(--rule-thick) solid var(--color-ink);
  }
  .tab:disabled {
    color: var(--color-subtle);
    cursor: not-allowed;
  }
  .tab:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: -2px;
  }
  .tab__short {
    display: none;
  }
  .tab__count {
    min-width: 1.5rem;
    padding: 0 var(--space-2);
    border-radius: var(--radius-pill);
    background: var(--color-surface-2);
    color: var(--color-ink);
    font: 500 var(--step--1) / 1.5 var(--font-ui);
    font-variant-numeric: tabular-nums;
    text-align: center;
  }
  .tab-panel {
    padding-top: var(--space-5);
  }
  .tab-panel:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }
  @media (max-width: 40rem) {
    .tabs {
      gap: var(--space-4);
    }
    /* the full label stays the accessible name, the short one is what is seen */
    .tab__long {
      position: absolute;
      inline-size: 1px;
      block-size: 1px;
      overflow: hidden;
      clip-path: inset(50%);
      white-space: nowrap;
    }
    .tab__short {
      display: inline;
    }
  }
</style>
