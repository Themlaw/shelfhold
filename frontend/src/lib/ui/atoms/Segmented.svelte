<script lang="ts" module>
  export type SegmentOption = { value: string; label: string; icon?: IconName; disabled?: boolean };
</script>
<script lang="ts">
  // A choice among a few, all visible at once: real radio buttons (Tab enters the group once, the
  // arrows move inside it) drawn as joined segments, the chosen one in full ink. The legend names
  // the group; hide it with `hideLegend` when the row already says what it is for.
  import Icon from './Icon.svelte';
  import type { IconName } from './icons.ts';

  let {
    name,
    legend,
    options,
    value = $bindable(),
    hideLegend = false,
    disabled = false,
    onchange
  }: {
    name: string;
    legend: string;
    options: readonly SegmentOption[];
    value?: string | null;
    hideLegend?: boolean;
    disabled?: boolean;
    onchange?: (value: string) => void;
  } = $props();
</script>

<fieldset class="segmented" {disabled}>
  <legend class:visually-hidden={hideLegend}>{legend}</legend>
  <div class="segmented__row">
    {#each options as option (option.value)}
      <label class="segment" class:segment--on={value === option.value} class:segment--off={option.disabled}>
        <input
          type="radio"
          {name}
          value={option.value}
          checked={value === option.value}
          disabled={option.disabled}
          onchange={() => {
            value = option.value;
            onchange?.(option.value);
          }}
        />
        {#if option.icon}<Icon name={option.icon} size="sm" />{/if}
        <span>{option.label}</span>
      </label>
    {/each}
  </div>
</fieldset>

<style>
  .segmented {
    margin: 0;
    padding: 0;
    border: 0;
    min-inline-size: 0;
  }
  legend {
    padding: 0;
    margin-bottom: var(--space-1);
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  .segmented__row {
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: 1fr;
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-radius: var(--radius-sm);
    overflow: hidden;
    background: var(--color-surface);
  }
  .segment {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    min-block-size: 44px;
    padding: var(--space-2) var(--space-3);
    text-align: center;
    line-height: 1.15;
    cursor: pointer;
    color: var(--color-ink);
    font-size: var(--step--1);
  }
  .segment + .segment {
    border-inline-start: var(--rule-hair) solid var(--color-rule-strong);
  }
  .segment:hover:not(.segment--on) {
    background: var(--color-surface-2);
  }
  .segment--on {
    background: var(--color-ink);
    color: var(--color-bg);
    font-weight: 600;
  }
  .segment input {
    position: absolute;
    inset: 0;
    margin: 0;
    opacity: 0;
    cursor: inherit;
  }
  .segment:has(input:focus-visible) {
    outline: 2px solid var(--color-focus);
    outline-offset: -3px;
  }
  .segment--off {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .segmented:disabled .segment {
    cursor: not-allowed;
    opacity: 0.6;
  }
  @media (max-width: 40rem) {
    .segment {
      flex-direction: column;
      gap: var(--space-1);
    }
  }
</style>
