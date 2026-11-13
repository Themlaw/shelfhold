<script lang="ts">
  type Cover = {
    id?: string;
    title?: string;
    cover?: string | null;
    cover_url?: string | null;
    thumbnail?: string | null;
  };

  let {
    items = [],
    covers,
    layout = 'grid'
  }: {
    items?: Cover[];
    covers?: Cover[];
    layout?: 'grid' | 'list';
  } = $props();

  const list = $derived((covers ?? items) as Cover[]);
</script>

<section class="cover-grid" data-layout={layout}>
  <h2>Couvertures</h2>
  <div class={layout === 'list' ? 'list' : 'grid'}>
    {#each list as item (item.id ?? item.title)}
      <article class="cover">
        {#if (item.cover_url ?? item.cover ?? item.thumbnail)}
          <img src={item.cover_url ?? item.cover ?? item.thumbnail} alt={item.title ?? ''} loading="lazy" />
        {:else}
          <div class="placeholder" aria-hidden="true"></div>
        {/if}
        <p>{item.title ?? ''}</p>
      </article>
    {/each}
  </div>
</section>

<style>
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(8rem, 1fr)); gap: 1rem; }
  .list { display: flex; flex-direction: column; gap: 0.5rem; }
  .cover img { width: 100%; height: auto; }
  .placeholder { width: 100%; aspect-ratio: 2/3; background: var(--surface-2, #eee); }
</style>
