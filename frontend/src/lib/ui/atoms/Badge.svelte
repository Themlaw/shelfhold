<script lang="ts" module>
  export type BadgeTone = 'neutral' | 'accent' | 'success' | 'warning' | 'danger' | 'solid' | 'count' | 'age';
</script>

<script lang="ts">
  // A small label. A status is an icon and a word, never a colour alone: success, warning and
  // danger carry their icon unless `icon={null}`. `count` is a round number (« 3 »), `age` the
  // cartouche of an age rating (« 13+ »).
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';
  import type { IconName } from './icons.ts';

  const DEFAULT_ICON: Partial<Record<BadgeTone, IconName>> = {
    success: 'check',
    warning: 'alert',
    danger: 'alert'
  };

  let {
    tone = 'neutral',
    icon,
    label,
    children
  }: { tone?: BadgeTone; icon?: IconName | null; label?: string; children: Snippet } = $props();

  const shown = $derived(icon === null ? null : (icon ?? DEFAULT_ICON[tone] ?? null));
</script>

<span class="badge badge--{tone}" aria-label={label}>
  {#if shown}<Icon name={shown} size="sm" />{/if}
  {@render children()}
</span>

<style>
  .badge {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: 0.0625rem var(--space-2);
    font: 500 var(--step--1) / 1.5 var(--font-ui);
    white-space: nowrap;
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-radius: var(--radius-xs);
    color: var(--color-muted);
    background: transparent;
  }
  .badge--accent {
    color: var(--color-accent-text);
    border-color: currentColor;
    background: var(--color-accent-soft);
  }
  .badge--success {
    color: var(--color-ok);
    border-color: currentColor;
    background: var(--color-ok-soft);
  }
  .badge--warning {
    color: var(--color-warn);
    border-color: currentColor;
    background: var(--color-warn-soft);
  }
  .badge--danger {
    color: var(--color-err);
    border-color: currentColor;
    background: var(--color-err-soft);
  }
  .badge--solid {
    color: var(--color-on-accent);
    background: var(--color-accent);
    border-color: var(--color-accent);
  }
  .badge--count {
    min-width: 1.5rem;
    justify-content: center;
    border-radius: var(--radius-pill);
    color: var(--color-on-accent);
    background: var(--color-accent);
    border-color: var(--color-accent);
    font-variant-numeric: tabular-nums;
  }
  .badge--age {
    font-family: var(--font-text);
    font-style: italic;
    font-weight: 600;
    color: var(--color-ink);
    border-color: var(--color-ink);
    border-width: var(--rule-mid);
  }
</style>
