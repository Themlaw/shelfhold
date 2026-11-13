<script lang="ts">
  import { onMount } from 'svelte';
  import { editionsList } from '#lib/api/index.ts';
  import CoverGrid from '#lib/components/CoverGrid.svelte';
  import { client } from '#lib/api/client.gen.ts';

  let layout = $state<'grid' | 'list'>('grid');
  let items = $state<any[]>([]);
  let loaded = $state(false);

  onMount(async () => {
    try {
      const { data } = await editionsList({ throwOnError: true });
      items = (data.items ?? []).map((e: any) => ({ id: e.edition_id, title: e.title }));
    } catch {
      items = [];
    }
    loaded = true;
  });
</script>

<section>
  <h1>Bibliothèque</h1>
  <div class="toolbar">
    <button type="button" onclick={() => (layout = 'grid')}>Grille</button>
    <button type="button" onclick={() => (layout = 'list')}>Liste</button>
  </div>
  <CoverGrid {items} {layout} />
  {#if !loaded}
    <p>Chargement…</p>
  {/if}
</section>
