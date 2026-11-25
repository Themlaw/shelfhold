<script lang="ts">
  // A placeholder with the exact shape of what comes: a line of text, a cover (2:3) or a block.
  // Always hidden from assistive technology: the region that loads says « Chargement… » itself.
  let { shape = 'line', short = false }: { shape?: 'line' | 'cover' | 'block'; short?: boolean } =
    $props();
</script>

<span class="skeleton skeleton--{shape}" class:short aria-hidden="true"></span>

<style>
  .skeleton {
    display: block;
    background: var(--color-surface-2);
    border-radius: var(--radius-xs);
    animation: breathe 1.6s var(--ease-in-out) infinite alternate;
  }
  .skeleton--line {
    height: 0.8em;
    width: 70%;
    margin-top: var(--space-2);
  }
  .skeleton--line.short {
    width: 42%;
  }
  .skeleton--cover {
    width: 100%;
    aspect-ratio: 2 / 3;
    border-radius: var(--radius-cover);
  }
  .skeleton--block {
    width: 100%;
    min-height: 4rem;
  }
  @keyframes breathe {
    from {
      opacity: 1;
    }
    to {
      opacity: 0.55;
    }
  }
</style>
