<script lang="ts">
  // An on/off setting that applies at once (role="switch"): the label is on the left, the switch
  // on the right, and the whole row is the target.
  import type { HTMLInputAttributes } from 'svelte/elements';

  let {
    checked = $bindable(false),
    label,
    hint,
    class: className = '',
    ...rest
  }: { checked?: boolean; label: string; hint?: string } & Omit<
    HTMLInputAttributes,
    'type' | 'checked' | 'role'
  > = $props();

  const hintId = $props.id();
</script>

<label class="switch-row {className}">
  <span class="switch-row__text">
    <span id="{hintId}-label">{label}</span>
    {#if hint}<span class="hint" id="{hintId}-hint">{hint}</span>{/if}
  </span>
  <input
    {...rest}
    type="checkbox"
    role="switch"
    class="switch"
    bind:checked
    aria-checked={checked}
    aria-labelledby="{hintId}-label"
    aria-describedby={hint ? `${hintId}-hint` : undefined}
  />
</label>

<style>
  .switch-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-4);
    min-height: var(--target);
    cursor: pointer;
  }
  .switch-row__text {
    display: grid;
    gap: var(--space-1);
  }
  .switch {
    appearance: none;
    flex: none;
    position: relative;
    width: 2.75rem;
    height: 1.5rem;
    margin: 0;
    cursor: pointer;
    background: var(--color-surface-2);
    border: var(--rule-mid) solid var(--color-rule-strong);
    border-radius: var(--radius-pill);
    transition: background-color var(--dur-2) var(--ease-out);
  }
  .switch::before {
    content: '';
    position: absolute;
    top: 0.125rem;
    left: 0.125rem;
    width: 1rem;
    height: 1rem;
    border-radius: var(--radius-pill);
    background: var(--color-muted);
    transition: transform var(--dur-2) var(--ease-out);
  }
  .switch:checked {
    background: var(--color-accent);
    border-color: var(--color-accent);
  }
  .switch:checked::before {
    transform: translateX(1.25rem);
    background: var(--color-on-accent);
  }
  .switch:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .switch:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }
  .hint {
    color: var(--color-muted);
    font-size: var(--step--1);
  }
</style>
