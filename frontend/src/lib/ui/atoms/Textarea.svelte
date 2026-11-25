<script lang="ts">
  // A text area: the look of Input, resizable in height only.
  import type { HTMLTextareaAttributes } from 'svelte/elements';

  let {
    value = $bindable(),
    invalid = false,
    class: className = '',
    ...rest
  }: { value?: string | null; invalid?: boolean } & Omit<HTMLTextareaAttributes, 'value'> = $props();
</script>

<textarea
  {...rest}
  bind:value
  class="textarea {className}"
  aria-invalid={invalid || rest['aria-invalid'] || undefined}
></textarea>

<style>
  .textarea {
    display: block;
    width: 100%;
    min-height: 7rem;
    padding: var(--space-2) var(--space-3);
    font: var(--step-0) / var(--leading-ui) var(--font-ui);
    background: var(--color-surface);
    color: var(--color-ink);
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-radius: var(--radius-sm);
    resize: vertical;
  }
  .textarea::placeholder {
    color: var(--color-subtle);
    font-style: italic;
    opacity: 1;
  }
  .textarea:hover {
    border-color: var(--color-ink);
  }
  .textarea:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 0;
    border-color: var(--color-focus);
  }
  .textarea[aria-invalid='true'] {
    border-color: var(--color-err);
    border-width: var(--rule-mid);
  }
  .textarea:disabled {
    background: var(--color-surface-2);
    color: var(--color-subtle);
  }
</style>
