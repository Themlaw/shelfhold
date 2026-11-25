<script lang="ts">
  // One radio button in its label. `group` is the value of the group (bindable): this button is
  // checked when it equals `value`. Put several in a <fieldset> with a <legend>.
  import type { Snippet } from 'svelte';
  import type { HTMLInputAttributes } from 'svelte/elements';

  let {
    group = $bindable(),
    value,
    label,
    hint,
    children,
    class: className = '',
    ...rest
  }: {
    group?: string | null;
    value: string;
    label?: string;
    hint?: string;
    children?: Snippet;
  } & Omit<HTMLInputAttributes, 'type' | 'value' | 'checked' | 'children'> = $props();

  const hintId = $props.id();
</script>

<label class="choice {className}">
  <input
    {...rest}
    type="radio"
    class="radio"
    {value}
    checked={group === value}
    aria-describedby={hint ? `${hintId}-hint` : undefined}
    onchange={(event) => {
      if (event.currentTarget.checked) group = value;
      rest.onchange?.(event as never);
    }}
  />
  <span class="choice__text">
    <span>{#if children}{@render children()}{:else}{label}{/if}</span>
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
  .radio {
    appearance: none;
    flex: none;
    width: 1.25rem;
    height: 1.25rem;
    margin: 0.1rem 0 0;
    display: inline-grid;
    place-content: center;
    background: var(--color-surface);
    border: var(--rule-mid) solid var(--color-rule-strong);
    border-radius: var(--radius-pill);
    cursor: pointer;
  }
  .radio::before {
    content: '';
    width: 0.55rem;
    height: 0.55rem;
    border-radius: var(--radius-pill);
    background: var(--color-on-accent);
    transform: scale(0);
    transition: transform var(--dur-1) var(--ease-out);
  }
  .radio:checked {
    background: var(--color-accent);
    border-color: var(--color-accent);
  }
  .radio:checked::before {
    transform: scale(1);
  }
  .radio:hover {
    border-color: var(--color-ink);
  }
  .radio:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .radio:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }
  .hint {
    color: var(--color-muted);
    font-size: var(--step--1);
  }
</style>
