<script lang="ts" module>
  export type Column = {
    key: string;
    label: string;
    /** numbers: right-aligned, tabular figures */
    num?: boolean;
    /** the cell that names the row: no label above it on a phone */
    primary?: boolean;
    /** the actions of the row: right-aligned, no label on a phone */
    actions?: boolean;
    /** hidden header (an actions column) */
    hideLabel?: boolean;
  };
</script>

<script lang="ts">
  // A row of a register (DataTable). On a phone each row becomes a card: each cell is printed
  // under its label (`data-label`), nothing is cut and nothing scrolls in secret.
  import type { Snippet } from 'svelte';

  let {
    columns,
    row,
    selected = false,
    tone,
    cell
  }: {
    columns: readonly Column[];
    row: Record<string, unknown>;
    selected?: boolean;
    tone?: 'danger' | 'warning';
    cell?: Snippet<[Column, Record<string, unknown>]>;
  } = $props();
</script>

<tr aria-selected={selected || undefined} class={tone ? `is-${tone}` : undefined}>
  {#each columns as column (column.key)}
    <td
      data-label={column.primary || column.actions ? undefined : column.label}
      class:num={column.num}
      class:primary={column.primary}
      class:actions={column.actions}
    >
      {#if cell}{@render cell(column, row)}{:else}{String(row[column.key] ?? '')}{/if}
    </td>
  {/each}
</tr>
