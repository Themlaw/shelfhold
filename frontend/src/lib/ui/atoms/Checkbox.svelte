<script lang="ts">
  // A checkbox drawn by the house (appearance: none), wrapped in its label: the whole line is the
  // target. `indeterminate` is the mixed state of a « select all ».
  import type { Snippet } from 'svelte';
  import type { HTMLInputAttributes } from 'svelte/elements';

  let {
    checked = $bindable(false),
    indeterminate = false,
    label,
    hint,
    invalid = false,
    children,
    class: className = '',
    ...rest
  }: {
    checked?: boolean;
    indeterminate?: boolean;
    label?: string;
    hint?: string;
    invalid?: boolean;
    children?: Snippet;
  } & Omit<HTMLInputAttributes, 'type' | 'checked' | 'children'> = $props();

  const hintId = $props.id();
</script>

<label class="choice {className}">
  <input
    {...rest}
    type="checkbox"
    class="check"
    bind:checked
    {indeterminate}
    aria-invalid={invalid || undefined}
    aria-labelledby="{hintId}-label"
    aria-describedby={hint ? `${hintId}-hint` : rest['aria-describedby']}
  />
  <span class="choice__text">
    <span id="{hintId}-label">{#if children}{@render children()}{:else}{label}{/if}</span>
    {#if hint}<span class="hint" id="{hintId}-hint">{hint}</span>{/if}
  </span>
</label>

<style>
  .choice {
    display: flex;
    align-items: flex-start;
    gap: var(--space-3);
    min-height: var(--target);
    padding-block: var(--space-2);
    cursor: pointer;
  }
  .choice__text {
    display: grid;
    gap: var(--space-1);
  }
  .check {
    appearance: none;
    flex: none;
    width: 1.25rem;
    height: 1.25rem;
    margin: 0.1rem 0 0;
    display: inline-grid;
    place-content: center;
    background: var(--color-surface);
    border: var(--rule-mid) solid var(--color-rule-strong);
    border-radius: var(--radius-xs);
    cursor: pointer;
    transition: background-color var(--dur-1) var(--ease-out);
  }
  .check::before {
    content: '';
    width: 0.7rem;
    height: 0.4rem;
    border: solid var(--color-on-accent);
    border-width: 0 0 var(--rule-mid) var(--rule-mid);
    transform: translateY(-15%) rotate(-45deg) scale(0);
    transition: transform var(--dur-1) var(--ease-out);
  }
  .check:checked,
  .check:indeterminate {
    background: var(--color-accent);
    border-color: var(--color-accent);
  }
  .check:checked::before {
    transform: translateY(-15%) rotate(-45deg) scale(1);
  }
  .check:indeterminate::before {
    width: 0.6rem;
    height: 0;
    border-width: 0 0 var(--rule-mid) 0;
    transform: none;
  }
  .check:hover {
    border-color: var(--color-ink);
  }
  .check:checked:hover {
    border-color: var(--color-accent);
  }
  .check:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .check[aria-invalid='true'] {
    border-color: var(--color-err);
  }
  .check:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }
  .hint {
    color: var(--color-muted);
    font-size: var(--step--1);
  }
</style>
