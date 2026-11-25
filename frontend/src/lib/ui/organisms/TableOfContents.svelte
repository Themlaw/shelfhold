<script lang="ts" module>
  import type { IconName } from '../atoms/icons.ts';

  export type TocEntry = {
    href: string;
    label: string;
    icon?: IconName;
    /** the folio at the end of the leader dots: a count (« 1 284 », « 3 ») */
    folio?: string | number | null;
    /** the folio waits for an action (« 3 to approve »): blue and bold, said in words */
    alert?: boolean;
  };
  export type TocGroup = { label: string; entries: readonly TocEntry[] };
</script>

<script lang="ts">
  // The navigation is the table of contents of a book (the second signature of the direction):
  // entries in Alegreya Sans, leader dots, folios in old-style figures. The entry of the screen
  // shown carries aria-current="page" and a thick rule. `foot` holds the quiet actions (the
  // scheme, signing out).
  import type { Snippet } from 'svelte';
  import { formatNumber, t } from '#lib/i18n/index.ts';
  import Icon from '../atoms/Icon.svelte';

  let {
    groups,
    current = null,
    title,
    label,
    foot,
    onnavigate
  }: {
    groups: readonly TocGroup[];
    /** the href of the entry of the current screen */
    current?: string | null;
    title?: string;
    label: string;
    foot?: Snippet;
    /** called when an entry is followed (the sheet of a phone closes) */
    onnavigate?: () => void;
  } = $props();

  const folio = (value: string | number) => (typeof value === 'number' ? formatNumber(value) : value);
</script>

<nav class="toc-nav" aria-label={label}>
  {#if title}<p class="toc-nav__title">{title}</p>{/if}
  {#each groups as group (group.label)}
    <div class="toc-nav__group">
      <p class="toc-nav__label">{group.label}</p>
      <ul class="toc-nav__list">
        {#each group.entries as entry (entry.href)}
          <li>
            <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
            <a class="toc-link" href={entry.href} aria-current={entry.href === current ? 'page' : undefined} onclick={() => onnavigate?.()}>
              {#if entry.icon}<Icon name={entry.icon} />{/if}
              <span class="toc-link__label">{entry.label}</span>
              <span class="toc-link__leader" aria-hidden="true"></span>
              {#if entry.folio !== undefined && entry.folio !== null}
                <span class="toc-link__folio" class:toc-link__folio--alert={entry.alert}>
                  {folio(entry.folio)}{#if entry.alert}<span class="visually-hidden">{` ${t('ui.toc.waiting')}`}</span>{/if}
                </span>
              {/if}
            </a>
          </li>
        {/each}
      </ul>
    </div>
  {/each}
  {#if foot}<div class="toc-nav__foot">{@render foot()}</div>{/if}
</nav>

<style>
  .toc-nav__title {
    margin: 0 0 var(--space-3);
    font: italic 600 var(--step-1) / 1.2 var(--font-text);
  }
  .toc-nav__group + .toc-nav__group {
    margin-top: var(--space-5);
  }
  .toc-nav__label {
    margin: 0 0 var(--space-1);
    padding-left: var(--space-2);
    font: italic var(--step--1) / 1.2 var(--font-text);
    color: var(--color-muted);
  }
  .toc-nav__list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .toc-link {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    min-height: var(--target);
    padding: var(--space-2);
    border-radius: var(--radius-sm);
    color: var(--color-ink);
    text-decoration: none;
    font-size: var(--step-0);
  }
  .toc-link > :global(.icon) {
    align-self: center;
    color: var(--color-muted);
  }
  .toc-link__leader {
    flex: 1;
    min-width: var(--space-4);
    border-bottom: var(--rule-mid) dotted var(--color-rule-strong);
    transform: translateY(-0.3em);
  }
  .toc-link__folio {
    font: italic var(--step-0) / 1 var(--font-text);
    font-variant-numeric: oldstyle-nums;
    color: var(--color-muted);
  }
  .toc-link__folio--alert {
    color: var(--color-accent-text);
    font: 700 var(--step-0) / 1 var(--font-ui);
  }
  .toc-link:hover {
    background: var(--color-surface);
  }
  .toc-link[aria-current='page'] {
    font-weight: 700;
    background: var(--color-surface);
    box-shadow: inset var(--rule-thick) 0 0 var(--color-ink);
  }
  .toc-link[aria-current='page'] > :global(.icon) {
    color: var(--color-ink);
  }
  .toc-link:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: -2px;
  }
  .toc-nav__foot {
    display: grid;
    gap: var(--space-1);
    margin-top: var(--space-6);
    padding-top: var(--space-4);
    border-top: var(--rule-hair) solid var(--color-rule);
  }
</style>
