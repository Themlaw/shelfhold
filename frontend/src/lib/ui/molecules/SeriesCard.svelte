<script lang="ts">
  // A volume standing on the tablet (the signature of the direction): the cover on its shelf
  // rule, the title (two lines at most, the whole title in `title`), the author on one line, and
  // the meta. The whole card is one link (the title's), so it is one stop in the tab order. With
  // no `href` it is not a link (a child's locked work, a volume being prepared). Put cards in a
  // Shelf: the shelf rules join into one.
  import type { Snippet } from 'svelte';
  import { t } from '#lib/i18n/index.ts';
  import Cover from '../atoms/Cover.svelte';
  import type { CoverKind } from '../atoms/cover.ts';

  let {
    title,
    href = null,
    author = null,
    src = null,
    kind = 'book',
    number = null,
    progress = null,
    done = false,
    locked = false,
    headingLevel = 3,
    meta
  }: {
    title: string;
    href?: string | null;
    author?: string | null;
    src?: string | null;
    kind?: CoverKind;
    number?: string | number | null;
    progress?: number | null;
    done?: boolean;
    locked?: boolean;
    headingLevel?: 2 | 3 | 4;
    meta?: Snippet;
  } = $props();

  const shown = $derived(locked ? t('ui.cover.locked') : title);
</script>

<article class="volume">
  <div class="volume__stand">
    <Cover {title} {author} {src} {kind} {number} {progress} {done} {locked} />
  </div>
  <div class="volume__body">
    <svelte:element this={`h${headingLevel}`} class="volume__title">
      {#if href && !locked}
        <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
        <a {href} title={shown}>{shown}</a>
      {:else}
        <span title={shown}>{shown}</span>
      {/if}
    </svelte:element>
    {#if !locked}
      <p class="volume__author">{author ?? t('ui.cover.unknown_author')}</p>
    {/if}
    {#if meta}<div class="volume__meta">{@render meta()}</div>{/if}
  </div>
</article>

<style>
  .volume {
    position: relative;
    min-width: 0;
  }
  .volume__stand {
    position: relative;
    padding: var(--space-4) var(--space-3) 0;
    border-bottom: var(--rule-thick) solid var(--color-shelf);
  }
  .volume__stand::after {
    content: '';
    position: absolute;
    left: 0;
    right: 0;
    bottom: calc(-1 * (var(--rule-thick) + var(--space-1) + var(--rule-hair)));
    border-bottom: var(--rule-hair) solid var(--color-shelf);
  }
  .volume__stand :global(.cover) {
    transition: transform var(--dur-2) var(--ease-out);
  }
  .volume:hover .volume__stand :global(.cover) {
    transform: translateY(calc(-1 * var(--space-1)));
  }
  .volume:has(a:focus-visible) .volume__stand :global(.cover) {
    outline: 2px solid var(--color-focus);
    outline-offset: 3px;
  }
  .volume__body {
    display: grid;
    gap: var(--space-1);
    padding: var(--space-3) var(--space-3) 0;
  }
  .volume__title {
    margin: 0;
    font: 600 var(--step-05) / 1.25 var(--font-text);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .volume__title a {
    color: var(--color-ink);
    text-decoration: none;
  }
  .volume__title a::after {
    content: '';
    position: absolute;
    inset: 0;
  }
  .volume__title a:focus-visible {
    outline: none;
  }
  .volume__author {
    margin: 0;
    font: italic var(--step--1) / 1.3 var(--font-text);
    color: var(--color-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .volume__meta {
    position: relative;
    z-index: 1;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1) var(--space-2);
    font-size: var(--step--1);
    color: var(--color-muted);
  }
  @media (max-width: 40rem) {
    .volume__title {
      -webkit-line-clamp: 3;
      line-clamp: 3;
    }
  }
</style>
