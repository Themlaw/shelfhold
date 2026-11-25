<script lang="ts">
  // A turning rule. With a label it is a live status (« Chargement… »); without, it is decoration
  // next to a text that already says what is happening.
  let { label, size = 'md' }: { label?: string; size?: 'sm' | 'md' } = $props();
</script>

{#if label}
  <span class="spinner-wrap" role="status">
    <span class="spinner spinner--{size}" aria-hidden="true"></span>
    <span class="visually-hidden">{label}</span>
  </span>
{:else}
  <span class="spinner spinner--{size}" aria-hidden="true"></span>
{/if}

<style>
  .spinner-wrap {
    display: inline-flex;
    align-items: center;
  }
  .spinner {
    display: inline-block;
    flex: none;
    width: 1.25rem;
    height: 1.25rem;
    border-radius: var(--radius-pill);
    border: var(--rule-mid) solid var(--color-rule);
    border-top-color: var(--color-accent);
    animation: spin 0.8s linear infinite;
  }
  .spinner--sm {
    width: 1em;
    height: 1em;
    border-color: currentColor;
    border-right-color: transparent;
  }
  @keyframes spin {
    to {
      transform: rotate(1turn);
    }
  }
</style>
