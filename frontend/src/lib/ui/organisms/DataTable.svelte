<script lang="ts" generics="Row extends Record<string, unknown>">
  // A register: a caption in italics, a thick rule on top, sticky headers, numbers on the
  // right in tabular figures, no vertical rules. Under 600 px each row is a card (TableRow):
  // the cells print under their labels. `cell` draws a cell (a badge, a link, the actions);
  // without it the value is printed as text. `empty` is drawn when there is no row.
  import type { Snippet } from 'svelte';
  import TableRow, { type Column } from '../molecules/TableRow.svelte';

  let {
    caption,
    captionHidden = false,
    columns,
    rows,
    key,
    selected,
    tone,
    compact = false,
    busy = false,
    cell,
    empty
  }: {
    caption: string;
    captionHidden?: boolean;
    columns: readonly Column[];
    rows: readonly Row[];
    /** the identity of a row */
    key: (row: Row) => string | number;
    selected?: (row: Row) => boolean;
    tone?: (row: Row) => 'danger' | 'warning' | undefined;
    compact?: boolean;
    busy?: boolean;
    cell?: Snippet<[Column, Row]>;
    empty?: Snippet;
  } = $props();
</script>

{#if rows.length === 0 && empty && !busy}
  {@render empty()}
{:else}
  <div class="ledger-wrap">
    <table class="ledger" class:ledger--compact={compact} aria-busy={busy || undefined}>
      <caption class:visually-hidden={captionHidden}>{caption}</caption>
      <thead>
        <tr>
          {#each columns as column (column.key)}
            <th scope="col" class:num={column.num} class:actions={column.actions}>
              {#if column.hideLabel}<span class="visually-hidden">{column.label}</span>{:else}{column.label}{/if}
            </th>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#each rows as row (key(row))}
          <TableRow
            {columns}
            {row}
            selected={selected?.(row) ?? false}
            tone={tone?.(row)}
            cell={cell as Snippet<[Column, Record<string, unknown>]> | undefined}
          />
        {/each}
      </tbody>
    </table>
  </div>
{/if}

<style>
  .ledger-wrap {
    border-top: var(--rule-thick) solid var(--color-ink);
  }
  .ledger {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--step-0);
  }
  caption {
    padding: var(--space-3) 0;
    font: italic var(--step-1) / 1.2 var(--font-text);
    text-align: left;
    caption-side: top;
  }
  th {
    position: sticky;
    top: var(--header-h);
    z-index: 1;
    padding: var(--space-3);
    font-size: var(--step--1);
    font-weight: 700;
    color: var(--color-muted);
    text-align: left;
    background: var(--color-bg);
    border-bottom: var(--rule-hair) solid var(--color-ink);
  }
  th.num {
    text-align: right;
  }
  th.actions {
    text-align: right;
  }
  .ledger :global(td) {
    padding: var(--space-3);
    vertical-align: middle;
    border-bottom: var(--rule-hair) solid var(--color-rule);
  }
  .ledger--compact :global(td),
  .ledger--compact th {
    padding-block: var(--space-2);
  }
  .ledger :global(td.num) {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .ledger :global(td.actions) {
    text-align: right;
    white-space: nowrap;
  }
  .ledger :global(tbody tr:hover) {
    background: var(--color-surface);
  }
  .ledger :global(tr[aria-selected='true']) {
    background: var(--color-accent-soft);
  }
  .ledger :global(tr.is-danger) {
    box-shadow: inset var(--rule-thick) 0 0 var(--color-err);
  }
  .ledger :global(tr.is-warning) {
    box-shadow: inset var(--rule-thick) 0 0 var(--color-warn);
  }
  /* a phone: each row is a card, each cell under its label (ENF-A11Y-004) */
  @media (max-width: 599.98px) {
    .ledger-wrap {
      border-top-width: var(--rule-mid);
    }
    .ledger :global(tr) {
      margin: 0;
      padding: var(--space-3) 0;
      border: 0;
      border-bottom: var(--rule-hair) solid var(--color-rule);
      border-radius: 0;
    }
    .ledger :global(td) {
      display: grid;
      grid-template-columns: 7.5rem minmax(0, 1fr);
      gap: var(--space-3);
      padding: var(--space-1) 0;
      text-align: left;
      border: 0;
    }
    .ledger :global(td[data-label]::before) {
      content: attr(data-label);
      font-size: var(--step--1);
      font-weight: 400;
      color: var(--color-muted);
    }
    .ledger :global(td.num) {
      text-align: left;
    }
    .ledger :global(td.primary),
    .ledger :global(td.actions) {
      grid-template-columns: minmax(0, 1fr);
    }
    .ledger :global(td.actions) {
      padding-top: var(--space-2);
      white-space: normal;
    }
  }
</style>
