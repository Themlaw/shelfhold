<svelte:options namespace="svg" />

<script lang="ts">
  // The shapes of an icon, a motif or the mark, drawn as SVG elements (never an HTML string).
  import type { Shape } from './icons.ts';

  let { shapes }: { shapes: readonly Shape[] } = $props();
</script>

{#each shapes as shape, index (index)}
  {#if shape.tag === 'path'}
    <path d={shape.d} />
  {:else if shape.tag === 'circle'}
    <circle cx={shape.cx} cy={shape.cy} r={shape.r} />
  {:else if shape.tag === 'rect'}
    <rect x={shape.x} y={shape.y} width={shape.width} height={shape.height} rx={shape.rx} />
  {:else}
    <ellipse cx={shape.cx} cy={shape.cy} rx={shape.rx} ry={shape.ry} />
  {/if}
{/each}
