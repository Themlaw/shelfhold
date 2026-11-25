<script lang="ts">
  // A search form (role="search"): a magnifier, a field with a hidden label, and, on a wide
  // screen, the « / » key that focuses it. Submitting calls `onsearch` with the trimmed text, or
  // follows `action` (a GET form) when there is no handler.
  import { t } from '#lib/i18n/index.ts';
  import Icon from '../atoms/Icon.svelte';

  let {
    value = $bindable(''),
    label,
    placeholder,
    action,
    name = 'q',
    size = 'md',
    shortcut = false,
    onsearch,
    class: className = ''
  }: {
    value?: string;
    label: string;
    placeholder?: string;
    action?: string;
    name?: string;
    size?: 'md' | 'lg';
    /** listen to « / » anywhere on the page to focus the field */
    shortcut?: boolean;
    onsearch?: (query: string) => void;
    class?: string;
  } = $props();

  const id = $props.id();
  let input = $state<HTMLInputElement | null>(null);

  function submit(event: SubmitEvent): void {
    if (!onsearch) return;
    event.preventDefault();
    onsearch(value.trim());
  }

  function key(event: KeyboardEvent): void {
    if (!shortcut || event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target as HTMLElement | null;
    if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;
    event.preventDefault();
    input?.focus();
  }
</script>

<svelte:window onkeydown={key} />

<form class="search search--{size} {className}" role="search" {action} method="get" onsubmit={submit}>
  <Icon name="search" />
  <label class="visually-hidden" for="search-{id}">{label}</label>
  <input
    bind:this={input}
    bind:value
    id="search-{id}"
    {name}
    type="search"
    class="search__input"
    {placeholder}
    autocomplete="off"
    enterkeyhint="search"
  />
  {#if shortcut}<span class="search__key" aria-hidden="true" title={t('ui.search.shortcut')}>/</span>{/if}
</form>

<style>
  .search {
    position: relative;
    display: flex;
    align-items: center;
    min-width: 0;
  }
  .search > :global(.icon) {
    position: absolute;
    left: var(--space-3);
    color: var(--color-muted);
    pointer-events: none;
  }
  .search__input {
    width: 100%;
    min-height: var(--target);
    padding: var(--space-2) var(--space-7) var(--space-2) calc(var(--space-3) * 2 + 1.25rem);
    font: var(--step-0) / var(--leading-ui) var(--font-ui);
    color: var(--color-ink);
    background: var(--color-surface);
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-radius: var(--radius-pill);
  }
  .search__input::placeholder {
    color: var(--color-subtle);
    font-style: italic;
    opacity: 1;
  }
  .search__input:hover {
    border-color: var(--color-ink);
  }
  .search__input:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 0;
    border-color: var(--color-focus);
  }
  .search--lg .search__input {
    min-height: 3.25rem;
    font-size: var(--step-1);
  }
  .search__key {
    position: absolute;
    right: var(--space-3);
    min-width: 1.6em;
    padding: 0 var(--space-1);
    text-align: center;
    font: var(--step--1) / 1.5 var(--font-mono);
    color: var(--color-muted);
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-bottom-width: var(--rule-mid);
    border-radius: var(--radius-xs);
  }
</style>
