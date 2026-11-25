<script lang="ts" module>
  export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'danger-ghost';
  export type ButtonSize = 'sm' | 'md' | 'lg';
</script>

<script lang="ts">
  // The one button of the house (docs/design/papier-et-encre.md). `href` makes it a link that
  // looks like a button (navigation); otherwise it is a <button>. Loading keeps the label and adds
  // a turning rule: the button says « Enregistrement… » itself, it is not emptied. A loading or
  // disabled button refuses the click but stays in the tab order with aria-disabled, so that a
  // keyboard user is not dropped when the state changes.
  import type { Snippet } from 'svelte';
  import type { HTMLAnchorAttributes, HTMLButtonAttributes } from 'svelte/elements';
  import Icon from './Icon.svelte';
  import type { IconName } from './icons.ts';

  type Common = {
    variant?: ButtonVariant;
    size?: ButtonSize;
    loading?: boolean;
    block?: boolean;
    icon?: IconName;
    iconEnd?: IconName;
    children: Snippet;
  };
  type Props = Common &
    (
      | ({ href: string } & Omit<HTMLAnchorAttributes, 'href' | 'children'>)
      | ({ href?: undefined } & Omit<HTMLButtonAttributes, 'children'>)
    );

  let {
    variant = 'secondary',
    size = 'md',
    loading = false,
    block = false,
    icon,
    iconEnd,
    children,
    class: className = '',
    ...rest
  }: Props = $props();
</script>

{#if rest.href !== undefined}
  {@const { href, ...link } = rest as { href: string } & HTMLAnchorAttributes}
  <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
  <a {href}
    {...link}
    class="btn btn--{variant} btn--{size} {className}"
    class:btn--block={block}
    aria-busy={loading || undefined}
  >
    {#if icon}<Icon name={icon} />{/if}
    {@render children()}
    {#if iconEnd}<Icon name={iconEnd} />{/if}
  </a>
{:else}
  {@const { disabled, onclick, type, ...button } = rest as HTMLButtonAttributes}
  <button
    {...button}
    type={type ?? 'button'}
    class="btn btn--{variant} btn--{size} {className}"
    class:btn--block={block}
    aria-busy={loading || undefined}
    aria-disabled={disabled || loading || undefined}
    onclick={(event) => {
      if (disabled || loading) {
        event.preventDefault();
        return;
      }
      onclick?.(event as never);
    }}
  >
    {#if loading}<span class="btn__busy" aria-hidden="true"></span>{:else if icon}<Icon name={icon} />{/if}
    {@render children()}
    {#if iconEnd && !loading}<Icon name={iconEnd} />{/if}
  </button>
{/if}

<style>
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    min-height: var(--target);
    min-width: var(--target);
    padding: var(--space-2) var(--space-4);
    font: 500 var(--step-0) / 1.1 var(--font-ui);
    text-decoration: none;
    text-align: center;
    white-space: nowrap;
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-radius: var(--radius-sm);
    background: var(--color-surface);
    color: var(--color-ink);
    cursor: pointer;
    transition:
      background-color var(--dur-1) var(--ease-out),
      border-color var(--dur-1) var(--ease-out),
      transform var(--dur-1) var(--ease-out);
  }
  .btn:hover {
    background: var(--color-surface-2);
    text-decoration: none;
  }
  .btn:active {
    transform: translateY(1px);
  }
  .btn:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }
  .btn--primary {
    background: var(--color-accent);
    border-color: var(--color-accent);
    color: var(--color-on-accent);
  }
  .btn--primary:hover {
    background: var(--color-accent-strong);
    border-color: var(--color-accent-strong);
  }
  .btn--secondary {
    background: transparent;
    border-color: var(--color-ink);
  }
  .btn--secondary:hover {
    background: var(--color-surface);
  }
  .btn--ghost {
    background: transparent;
    border-color: transparent;
    color: var(--color-accent-text);
  }
  .btn--ghost:hover {
    background: var(--color-accent-soft);
  }
  .btn--danger {
    background: var(--color-err);
    border-color: var(--color-err);
    color: var(--color-on-err);
  }
  .btn--danger:hover {
    background: color-mix(in oklab, var(--color-err), var(--color-ink) 18%);
  }
  .btn--danger-ghost {
    background: transparent;
    border-color: var(--color-err);
    color: var(--color-err);
  }
  .btn--danger-ghost:hover {
    background: var(--color-err-soft);
  }
  /* small: a quieter label, never a smaller target (ENF-A11Y-004) */
  .btn--sm {
    padding: var(--space-1) var(--space-3);
    font-size: var(--step--1);
  }
  .btn--lg {
    min-height: 3.25rem;
    padding: var(--space-3) var(--space-5);
    font-size: var(--step-1);
  }
  .btn--block {
    width: 100%;
  }
  .btn[aria-disabled='true'] {
    opacity: 0.55;
    cursor: not-allowed;
    transform: none;
  }
  .btn[aria-busy='true'] {
    cursor: progress;
    opacity: 0.85;
  }
  .btn__busy {
    width: 1em;
    height: 1em;
    border-radius: var(--radius-pill);
    border: var(--rule-mid) solid currentColor;
    border-right-color: transparent;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin {
    to {
      transform: rotate(1turn);
    }
  }
</style>
