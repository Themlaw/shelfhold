<script lang="ts" module>
  /** The page numbers to show around `page`, with `null` for a gap: 1 … 4 5 6 … 20. */
  export function pageWindow(page: number, pages: number): (number | null)[] {
    if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1);
    const out: (number | null)[] = [1];
    const from = Math.max(2, page - 1);
    const to = Math.min(pages - 1, page + 1);
    if (from > 2) out.push(null);
    for (let p = from; p <= to; p += 1) out.push(p);
    if (to < pages - 1) out.push(null);
    out.push(pages);
    return out;
  }
</script>

<script lang="ts">
  // Pages of a long list. The page is in the address (`href(page)`), so sharing and going back
  // work; or, with `onpage`, buttons. On a phone only « Previous / Next » and « Page 2 of 9 ».
  import { formatNumber, t } from '#lib/i18n/index.ts';
  import Icon from '../atoms/Icon.svelte';

  let {
    page,
    pages,
    href,
    onpage,
    label
  }: {
    page: number;
    pages: number;
    href?: (page: number) => string;
    onpage?: (page: number) => void;
    label?: string;
  } = $props();

  const items = $derived(pageWindow(page, pages));
</script>

{#snippet step(target: number, kind: 'prev' | 'next')}
  {@const text = kind === 'prev' ? t('ui.pagination.previous') : t('ui.pagination.next')}
  {#if target < 1 || target > pages}
    <span class="pagination__step is-off" aria-disabled="true">
      {#if kind === 'prev'}<Icon name="left" />{/if}{text}{#if kind === 'next'}<Icon name="right" />{/if}
    </span>
  {:else if href}
    <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- built by the screen from its own address -->
    <a class="pagination__step" href={href(target)} rel={kind}>
      {#if kind === 'prev'}<Icon name="left" />{/if}{text}{#if kind === 'next'}<Icon name="right" />{/if}
    </a>
  {:else}
    <button class="pagination__step" type="button" onclick={() => onpage?.(target)}>
      {#if kind === 'prev'}<Icon name="left" />{/if}{text}{#if kind === 'next'}<Icon name="right" />{/if}
    </button>
  {/if}
{/snippet}

{#if pages > 1}
  <nav class="pagination" aria-label={label ?? t('ui.pagination.label')}>
    {@render step(page - 1, 'prev')}
    <ul class="pagination__pages">
      {#each items as item, index (index)}
        <li>
          {#if item === null}
            <span class="pagination__gap" aria-hidden="true">…</span>
          {:else if item === page}
            <span class="pagination__num" aria-current="page">{formatNumber(item)}</span>
          {:else if href}
            <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- built by the screen from its own address -->
            <a class="pagination__num" href={href(item)} aria-label={t('ui.pagination.page', { page: item })}>{formatNumber(item)}</a>
          {:else}
            <button class="pagination__num" type="button" onclick={() => onpage?.(item)} aria-label={t('ui.pagination.page', { page: item })}>{formatNumber(item)}</button>
          {/if}
        </li>
      {/each}
    </ul>
    {@render step(page + 1, 'next')}
    <p class="pagination__count">{t('ui.pagination.count', { page, pages })}</p>
  </nav>
{/if}

<style>
  .pagination {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    margin-top: var(--space-7);
    font-family: var(--font-text);
  }
  .pagination__pages {
    display: flex;
    gap: var(--space-1);
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .pagination__num,
  .pagination__step {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-1);
    min-width: var(--target);
    min-height: var(--target);
    padding: 0 var(--space-2);
    border: var(--rule-hair) solid transparent;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--color-ink);
    text-decoration: none;
    cursor: pointer;
  }
  .pagination__num {
    font-size: var(--step-1);
    font-variant-numeric: oldstyle-nums;
  }
  .pagination__step {
    font: var(--step-0) / 1 var(--font-ui);
    padding-inline: var(--space-3);
  }
  .pagination__num:hover,
  .pagination__step:hover {
    background: var(--color-surface);
  }
  .pagination__num[aria-current] {
    font-weight: 700;
    border-color: var(--color-ink);
    cursor: default;
  }
  .pagination__step.is-off {
    color: var(--color-subtle);
    cursor: default;
  }
  .pagination__step.is-off:hover {
    background: none;
  }
  .pagination__gap {
    display: inline-grid;
    place-items: center;
    min-width: var(--space-6);
    min-height: var(--target);
    color: var(--color-subtle);
  }
  .pagination__count {
    width: 100%;
    margin: var(--space-2) 0 0;
    text-align: center;
    color: var(--color-muted);
    font-style: italic;
  }
  @media (max-width: 40rem) {
    .pagination__pages {
      display: none;
    }
    .pagination__count {
      order: -1;
    }
  }
</style>
