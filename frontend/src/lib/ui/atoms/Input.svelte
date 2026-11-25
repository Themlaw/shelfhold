<script lang="ts">
  // A text field. Its label is outside (Field), its width follows the data (`width`), it is at
  // 16 px so that a phone does not zoom on it. `invalid` sets aria-invalid; the message is given
  // by Field (aria-describedby).
  import type { HTMLInputAttributes } from 'svelte/elements';

  let {
    value = $bindable(),
    invalid = false,
    width = 'full',
    mono = false,
    class: className = '',
    ...rest
  }: {
    value?: string | number | null;
    invalid?: boolean;
    width?: 'short' | 'mid' | 'full';
    mono?: boolean;
  } & Omit<HTMLInputAttributes, 'value'> = $props();
</script>

<input
  {...rest}
  bind:value
  class="input input--{width} {className}"
  class:input--mono={mono}
  aria-invalid={invalid || rest['aria-invalid'] || undefined}
/>

<style>
  .input {
    display: block;
    width: 100%;
    min-height: var(--target);
    padding: var(--space-2) var(--space-3);
    font: var(--step-0) / var(--leading-ui) var(--font-ui);
    background: var(--color-surface);
    color: var(--color-ink);
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-radius: var(--radius-sm);
    transition: border-color var(--dur-1) var(--ease-out);
  }
  .input::placeholder {
    color: var(--color-subtle);
    font-style: italic;
    opacity: 1;
  }
  .input:hover {
    border-color: var(--color-ink);
  }
  .input:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 0;
    border-color: var(--color-focus);
  }
  .input[aria-invalid='true'] {
    border-color: var(--color-err);
    border-width: var(--rule-mid);
    padding-inline: calc(var(--space-3) - 1px);
  }
  .input:disabled {
    background: var(--color-surface-2);
    color: var(--color-subtle);
    cursor: not-allowed;
  }
  .input[readonly] {
    background: var(--color-bg);
    border-style: dashed;
  }
  .input--short {
    max-width: 9rem;
  }
  .input--mid {
    max-width: 22rem;
  }
  .input--mono {
    font-family: var(--font-mono);
    font-size: var(--step--1);
    letter-spacing: 0.02em;
  }
</style>
