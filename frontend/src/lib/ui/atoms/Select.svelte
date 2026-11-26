<script lang="ts" module>
  export type SelectOption = { value: string; label: string; disabled?: boolean };
</script>

<script lang="ts">
  // A choice in a list, drawn on the native <select>: it posts in a form, opens the picker of the
  // phone, and reads well with every screen reader. Give `options`, or <option> children.
  import type { Snippet } from 'svelte';
  import type { HTMLSelectAttributes } from 'svelte/elements';

  let {
    value = $bindable(),
    options,
    invalid = false,
    inline = false,
    children,
    class: className = '',
    ...rest
  }: {
    value?: string | null;
    options?: readonly SelectOption[];
    invalid?: boolean;
    inline?: boolean;
    children?: Snippet;
  } & Omit<HTMLSelectAttributes, 'value' | 'children'> = $props();
</script>

<span class="select-wrap" class:select-wrap--inline={inline}>
  <select
    {...rest}
    bind:value
    class="select {className}"
    aria-invalid={invalid || rest['aria-invalid'] || undefined}
  >
    {#if options}
      {#each options as option (option.value)}
        <option value={option.value} disabled={option.disabled}>{option.label}</option>
      {/each}
    {:else if children}
      {@render children()}
    {/if}
  </select>
</span>

<style>
  .select-wrap {
    position: relative;
    display: block;
  }
  .select-wrap--inline {
    display: inline-block;
    inline-size: fit-content;
    max-inline-size: 100%;
  }
  .select-wrap::after {
    content: '';
    position: absolute;
    right: var(--space-4);
    top: 50%;
    width: 0.5rem;
    height: 0.5rem;
    pointer-events: none;
    border: solid var(--color-muted);
    border-width: 0 var(--rule-mid) var(--rule-mid) 0;
    transform: translateY(-70%) rotate(45deg);
  }
  .select {
    appearance: none;
    display: block;
    width: 100%;
    min-height: var(--target);
    padding: var(--space-2) var(--space-7) var(--space-2) var(--space-3);
    font: var(--step-0) / var(--leading-ui) var(--font-ui);
    background: var(--color-surface);
    color: var(--color-ink);
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }
  .select:hover {
    border-color: var(--color-ink);
  }
  .select:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 0;
    border-color: var(--color-focus);
  }
  .select[aria-invalid='true'] {
    border-color: var(--color-err);
    border-width: var(--rule-mid);
  }
  .select:disabled {
    background: var(--color-surface-2);
    color: var(--color-subtle);
    cursor: not-allowed;
  }
  .select-wrap--inline .select {
    width: auto;
  }
</style>
