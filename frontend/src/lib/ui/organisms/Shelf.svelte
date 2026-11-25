<script lang="ts">
  // The shelf: volumes (SeriesCard) standing side by side on one continuous shelf rule. A grid
  // that fills the width (6 to 8 volumes at 1440, two on a phone); `row` keeps one row that
  // scrolls sideways with a snap (« Also reading »), `child` gives larger covers. An optional
  // heading and a « see all » link make it a section of a screen.
  import type { Snippet } from 'svelte';

  let {
    label,
    row = false,
    child = false,
    busy = false,
    children
  }: {
    /** the accessible name of the list (the heading of the section when there is one) */
    label: string;
    row?: boolean;
    child?: boolean;
    /** the volumes are loading: said to assistive technology */
    busy?: boolean;
    children: Snippet;
  } = $props();
</script>

<!-- a list of volumes: role="list" because Safari drops the role of a styled list -->
<div class="shelf" class:shelf--row={row} class:shelf--child={child} role="list" aria-label={label} aria-busy={busy || undefined}>
  {@render children()}
</div>

<style>
  .shelf {
    --min: var(--cover-min);
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(var(--min), 1fr));
    column-gap: 0;
    row-gap: var(--space-6);
    margin: 0;
    padding: 0;
  }
  .shelf--child {
    --min: var(--cover-min-kid);
  }
  .shelf--row {
    grid-template-columns: none;
    grid-auto-flow: column;
    grid-auto-columns: minmax(var(--min), 11rem);
    overflow-x: auto;
    overscroll-behavior-x: contain;
    scroll-snap-type: x mandatory;
    padding-bottom: var(--space-3);
  }
  .shelf > :global(*) {
    min-width: 0;
  }
  .shelf--row > :global(*) {
    scroll-snap-align: start;
  }
</style>
