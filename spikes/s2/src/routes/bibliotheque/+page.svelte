<script lang="ts">
  import { createVirtualizer } from '@tanstack/svelte-virtual';
  import { get } from 'svelte/store';
  import { t } from '#lib/i18n.ts';
  const items = Array.from({ length: 2000 }, (_, i) => ({ id: i, title: `Œuvre n° ${i + 1}` }));
  let el: HTMLDivElement;
  let rows = $state<{ index: number; start: number; size: number }[]>([]);
  let total = $state(0);
  $effect(() => {
    const v = createVirtualizer<HTMLDivElement, HTMLDivElement>({
      count: items.length, getScrollElement: () => el, estimateSize: () => 48, overscan: 8
    });
    const unsub = v.subscribe((s) => {
      rows = s.getVirtualItems().map((r) => ({ index: r.index, start: r.start, size: r.size }));
      total = s.getTotalSize();
    });
    return unsub;
  });
</script>
<svelte:head><title>Shelfhold</title></svelte:head>
<h1>{t('nav_lib')}</h1>
<p>{t('count', { n: items.length })}</p>
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div bind:this={el} data-testid="scroller" tabindex="0" role="region" aria-label="Liste des œuvres"
     style="height:480px;overflow:auto;background:#fffdf8">
  <div style="height:{total}px;position:relative">
    {#each rows as r (r.index)}
      <div data-row style="position:absolute;top:0;left:0;right:0;height:{r.size}px;transform:translateY({r.start}px)">
        {items[r.index].title}
      </div>
    {/each}
  </div>
</div>
