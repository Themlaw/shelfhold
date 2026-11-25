<svelte:options namespace="svg" />

<script lang="ts">
  // The elements of a cover scene (cover-art.ts), drawn as SVG elements: never an HTML string.
  import type { ArtNode } from './cover-art.ts';

  let { nodes }: { nodes: readonly ArtNode[] } = $props();
</script>

{#snippet draw(list: readonly ArtNode[])}
  {#each list as node, index (index)}
    {#if node.tag === 'path'}
      <path class={node.cls} d={node.d} />
    {:else if node.tag === 'circle'}
      <circle class={node.cls} cx={node.cx} cy={node.cy} r={node.r} />
    {:else if node.tag === 'rect'}
      <rect class={node.cls} x={node.x} y={node.y} width={node.width} height={node.height} />
    {:else}
      <g class={node.cls}>{@render draw(node.children)}</g>
    {/if}
  {/each}
{/snippet}

{@render draw(nodes)}
