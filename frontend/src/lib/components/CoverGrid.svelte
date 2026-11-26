<script lang="ts" module>
  export type CoverItem = {
    id: string;
    title: string;
    coverHash: string | null;
    href?: string | null;
  };
</script>

<script lang="ts">
  // The covers of the library, as a grid or a list (EF-BIB-020). The layout is the variant
  // `cover_layout` of the theme, read as the CSS property it is compiled to (I51): never a choice
  // of this component, never the name of a theme. A cover is the thumbnail the server made for it
  // (160 and 320 pixels): the original is never asked for, so 60 covers stay light (ENF-PERF-004).
  import { onMount } from 'svelte';
  import { variantProperty } from '#lib/themes/variants.ts';
  import Cover from '#lib/ui/atoms/Cover.svelte';

  let { items, label }: { items: CoverItem[]; label: string } = $props();

  let layout = $state<'grid' | 'list'>('grid');
  // a cover that fails to load, or that the server never made, is composed in type: never an empty
  // slot nor a broken picture
  let broken = $state<Record<string, boolean>>({});
  const HASH = /^[0-9a-f]{64}$/;
  const thumb = (hash: string, size: 160 | 320): string => `/api/v1/thumbnails/${hash}/${size}`;

  onMount(() => {
    const value = getComputedStyle(document.documentElement)
      .getPropertyValue(variantProperty('cover_layout'))
      .trim();
    layout = value === 'list' ? 'list' : 'grid';
  });
</script>

<ul class="covers" data-layout={layout} aria-label={label}>
  {#each items as item (item.id)}
    <li class="cover">
      <span class="stand">
        {#if item.coverHash && HASH.test(item.coverHash) && !broken[item.id]}
          <img
            src={thumb(item.coverHash, 160)}
            srcset={`${thumb(item.coverHash, 160)} 160w, ${thumb(item.coverHash, 320)} 320w`}
            sizes="160px"
            width="160"
            height="240"
            loading="lazy"
            alt={item.title}
            onerror={() => (broken[item.id] = true)}
          />
        {:else}
          <span class="composed"><Cover title={item.title} seed={item.id} /></span>
        {/if}
      </span>
      <span class="name">
        {#if item.href}
          <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
          <a href={item.href}>{item.title}</a>
        {:else}
          <span class="label">{item.title}</span>
        {/if}
      </span>
    </li>
  {/each}
</ul>

<style>
  .covers {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    column-gap: var(--space-4);
    row-gap: var(--space-6);
  }
  .covers[data-layout='grid'] {
    grid-template-columns: repeat(auto-fill, minmax(var(--cover-min), 1fr));
  }
  .covers[data-layout='list'] {
    grid-template-columns: minmax(0, 1fr);
    row-gap: var(--space-2);
  }
  .cover {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    min-width: 0;
  }
  [data-layout='list'] .cover {
    flex-direction: row;
    align-items: center;
    gap: var(--space-4);
    padding-bottom: var(--space-2);
    border-bottom: var(--rule-hair) solid var(--color-rule);
  }
  /* the volume stands on its shelf rule, as in the shelves of the other screens */
  .stand {
    display: block;
    padding: var(--space-3) var(--space-3) 0;
    border-bottom: var(--rule-thick) solid var(--color-shelf);
  }
  [data-layout='list'] .stand {
    flex: none;
    width: 4.5rem;
    padding: 0;
    border-bottom: 0;
  }
  img {
    display: block;
    width: 100%;
    height: auto;
    aspect-ratio: 2 / 3;
    border-radius: var(--radius-cover);
    object-fit: cover;
    box-shadow: var(--shadow-1);
  }
  .composed {
    display: block;
    width: 100%;
  }
  .name {
    min-width: 0;
    padding: 0 var(--space-3);
  }
  [data-layout='list'] .name {
    padding: 0;
  }
  .label,
  a {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    overflow-wrap: anywhere;
    font: 600 var(--step-05) / 1.25 var(--font-text);
  }
  /* ENF-A11Y-004: the title is the target of the cover, at least 44 px high */
  a {
    display: inline-flex;
    align-items: center;
    min-height: var(--target);
    color: var(--color-ink);
    text-decoration: none;
  }
  a:hover {
    text-decoration: underline;
  }
  a:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }
</style>
