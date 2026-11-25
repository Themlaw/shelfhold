<script lang="ts">
  // A button that is only an icon: its name is required (aria-label), and shown as a tooltip.
  // The target stays 44 by 44 px. `pressed` makes it a toggle (aria-pressed).
  import type { HTMLButtonAttributes } from 'svelte/elements';
  import Icon from './Icon.svelte';
  import type { IconName } from './icons.ts';

  let {
    icon,
    label,
    variant = 'ghost',
    pressed,
    class: className = '',
    ...rest
  }: {
    icon: IconName;
    label: string;
    variant?: 'ghost' | 'secondary' | 'primary';
    pressed?: boolean;
  } & Omit<HTMLButtonAttributes, 'children' | 'aria-label'> = $props();
</script>

<button
  type="button"
  {...rest}
  class="icon-btn icon-btn--{variant} {className}"
  aria-label={label}
  title={label}
  aria-pressed={pressed}
>
  <Icon name={icon} />
</button>

<style>
  .icon-btn {
    display: inline-grid;
    place-items: center;
    flex: none;
    width: var(--target);
    height: var(--target);
    padding: 0;
    border: var(--rule-hair) solid transparent;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--color-accent-text);
    cursor: pointer;
    transition: background-color var(--dur-1) var(--ease-out);
  }
  .icon-btn:hover {
    background: var(--color-accent-soft);
  }
  .icon-btn--secondary {
    border-color: var(--color-ink);
    color: var(--color-ink);
  }
  .icon-btn--secondary:hover {
    background: var(--color-surface);
  }
  .icon-btn--primary {
    background: var(--color-accent);
    color: var(--color-on-accent);
  }
  .icon-btn--primary:hover {
    background: var(--color-accent-strong);
  }
  .icon-btn[aria-pressed='true'] {
    background: var(--color-ink);
    color: var(--color-bg);
  }
  .icon-btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .icon-btn:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }
</style>
