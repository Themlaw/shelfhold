<script lang="ts">
  // The bookmark ribbon (the signature of the direction): a ribbon of blue ink hanging from the
  // top of a cover, as long as the reading went; green once the book is read. It sits inside a
  // positioned parent (Cover). It is a picture of a number said elsewhere in words, so it is
  // hidden from assistive technology unless `label` is given.
  let {
    value,
    done = false,
    side = 'right',
    label
  }: { value: number; done?: boolean; side?: 'left' | 'right'; label?: string } = $props();

  // at least a visible tab, at most the height of the cover
  const length = $derived(done ? 100 : Math.round(Math.min(1, Math.max(0.06, Number.isFinite(value) ? value : 0)) * 100));
</script>

<span
  class="ribbon ribbon--{side}"
  class:ribbon--done={done}
  style:block-size="{length}%"
  role={label ? 'img' : undefined}
  aria-label={label}
  aria-hidden={label ? undefined : 'true'}
  data-length={length}
></span>

<style>
  .ribbon {
    position: absolute;
    top: 0;
    z-index: 2;
    width: 6%;
    min-width: 0.4rem;
    background: var(--color-accent);
    clip-path: polygon(0 0, 100% 0, 100% 100%, 50% calc(100% - 0.35rem), 0 100%);
    border-inline: var(--rule-hair) solid color-mix(in oklab, var(--color-accent), black 25%);
  }
  .ribbon--right {
    right: 3.5%;
  }
  .ribbon--left {
    left: 3.5%;
  }
  .ribbon--done {
    background: var(--color-ok);
    border-color: color-mix(in oklab, var(--color-ok), black 25%);
  }
</style>
