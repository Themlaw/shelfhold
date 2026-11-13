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

  let { items, label }: { items: CoverItem[]; label: string } = $props();

  let layout = $state<'grid' | 'list'>('grid');
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
      {#if item.coverHash && HASH.test(item.coverHash)}
        <img
          src={thumb(item.coverHash, 160)}
          srcset={`${thumb(item.coverHash, 160)} 160w, ${thumb(item.coverHash, 320)} 320w`}
          sizes="160px"
          width="160"
          height="240"
          loading="lazy"
          alt={item.title}
        />
      {:else}
        <span class="blank" aria-hidden="true"></span>
      {/if}
      {#if item.href}
        <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
        <a href={item.href}>{item.title}</a>
      {:else}
        <span>{item.title}</span>
      {/if}
    </li>
  {/each}
</ul>

<style>
  .covers {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 1rem;
  }
  .covers[data-layout='grid'] {
    grid-template-columns: repeat(auto-fill, minmax(8rem, 1fr));
  }
  .covers[data-layout='list'] {
    grid-template-columns: 1fr;
    gap: 0.5rem;
  }
  .cover {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  [data-layout='list'] .cover {
    flex-direction: row;
    align-items: center;
    gap: 1rem;
  }
  /* ENF-A11Y-004: the title is the target of the cover, at least 44 px high */
  a {
    display: inline-flex;
    align-items: center;
    min-height: 2.75rem;
  }
  img,
  .blank {
    width: 100%;
    max-width: 10rem;
    height: auto;
    aspect-ratio: 2 / 3;
    border-radius: var(--radius-cover);
    background: var(--color-rule);
    object-fit: cover;
  }
  [data-layout='list'] img,
  [data-layout='list'] .blank {
    width: 4rem;
  }
</style>
