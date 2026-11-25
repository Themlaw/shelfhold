<script lang="ts">
  // An icon of the house set (icons.ts). Decorative by default (hidden from assistive technology):
  // the word next to it says the thing. Give `label` only when the icon stands alone.
  import { ICONS, type IconName } from './icons.ts';
  import Shapes from './Shapes.svelte';

  let {
    name,
    size = 'md',
    label,
    class: className = ''
  }: { name: IconName; size?: 'sm' | 'md' | 'lg' | 'xl'; label?: string; class?: string } = $props();
</script>

<svg
  class="icon icon--{size} {className}"
  viewBox="0 0 24 24"
  role={label ? 'img' : undefined}
  aria-label={label}
  aria-hidden={label ? undefined : 'true'}
  focusable="false"
  data-icon={name}
>
  <Shapes shapes={ICONS[name]} />
</svg>

<style>
  .icon {
    flex: none;
    width: 1.25rem;
    height: 1.25rem;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.6;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .icon--sm {
    width: 1rem;
    height: 1rem;
    stroke-width: 2;
  }
  .icon--lg {
    width: 1.75rem;
    height: 1.75rem;
  }
  .icon--xl {
    width: 3rem;
    height: 3rem;
    stroke-width: 1.2;
  }
</style>
