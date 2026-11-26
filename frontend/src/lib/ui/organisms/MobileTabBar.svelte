<script lang="ts" module>
  import type { IconName } from '../atoms/icons.ts';

  export type TabBarEntry = { href: string; label: string; icon: IconName; dot?: number | null };
</script>

<script lang="ts">
  // The bar at the bottom of a phone (under 60 rem): four places and a « More » button that opens
  // the whole table of contents. The current tab has aria-current and a thick rule above it.
  // Every target is 44 px high at least.
  import { formatNumber, t } from '#lib/i18n/index.ts';
  import Icon from '../atoms/Icon.svelte';

  let {
    entries,
    current = null,
    label,
    moreLabel,
    moreOpen = false,
    onmore,
    comfortable = false
  }: {
    entries: readonly TabBarEntry[];
    current?: string | null;
    label: string;
    moreLabel?: string;
    moreOpen?: boolean;
    onmore?: () => void;
    comfortable?: boolean;
  } = $props();
</script>

<nav class="tabbar" class:tabbar--comfortable={comfortable} aria-label={label}>
  {#each entries as entry (entry.href)}
    <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
    <a class="tabbar__item" href={entry.href} aria-current={entry.href === current ? 'page' : undefined}>
      <Icon name={entry.icon} size="lg" />
      <span>{entry.label}</span>
      {#if entry.dot}<span class="tabbar__dot">{formatNumber(entry.dot)}<span class="visually-hidden">{` ${t('ui.toc.waiting')}`}</span></span>{/if}
    </a>
  {/each}
  {#if onmore}
    <button class="tabbar__item" type="button" aria-expanded={moreOpen} aria-haspopup="dialog" onclick={onmore}>
      <Icon name="menu" size="lg" />
      <span>{moreLabel ?? t('nav.more')}</span>
    </button>
  {/if}
</nav>

<style>
  .tabbar {
    display: none;
  }
  @media (max-width: 60rem) {
    .tabbar {
      display: grid;
      grid-auto-columns: minmax(0, 1fr);
      grid-auto-flow: column;
      position: fixed;
      inset: auto 0 0 0;
      z-index: var(--z-sticky);
      min-height: var(--tabbar-h);
      padding: 0 var(--space-1) env(safe-area-inset-bottom);
      background: var(--color-surface-chrome);
      border-top: var(--rule-hair) solid var(--color-ink);
    }
  }
  .tabbar__item {
    position: relative;
    display: grid;
    justify-items: center;
    align-content: center;
    gap: 0.125rem;
    min-height: var(--target);
    padding: var(--space-2) 0;
    border: 0;
    background: none;
    font: 500 var(--step--2) / 1.2 var(--font-ui);
    color: var(--color-muted);
    text-decoration: none;
    cursor: pointer;
  }
  .tabbar--comfortable .tabbar__item {
    min-height: 48px;
  }
  .tabbar__item > :global(.icon) {
    width: 1.5rem;
    height: 1.5rem;
  }
  .tabbar__item[aria-current='page'] {
    color: var(--color-ink);
    font-weight: 700;
  }
  .tabbar__item[aria-current='page']::before {
    content: '';
    position: absolute;
    top: calc(-1 * var(--rule-hair));
    left: 22%;
    right: 22%;
    border-top: var(--rule-thick) solid var(--color-ink);
  }
  .tabbar__item:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: -2px;
  }
  .tabbar__dot {
    position: absolute;
    top: var(--space-1);
    left: calc(50% + 0.5rem);
    min-width: 1.125rem;
    height: 1.125rem;
    padding: 0 0.25rem;
    display: grid;
    place-items: center;
    border-radius: var(--radius-pill);
    background: var(--color-accent);
    color: var(--color-on-accent);
    font-size: var(--step--2);
    font-weight: 700;
  }
</style>
