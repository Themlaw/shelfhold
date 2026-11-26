<script lang="ts">
  // One setting: the label and a sentence of help on the left, the control on the right; one
  // column under 48 rem. `children` receives the id of the control and of its help, so the
  // label names it and the help describes it.
  import type { Snippet } from 'svelte';

  let {
    label,
    meta,
    help,
    children
  }: {
    label: string;
    meta?: string;
    help?: string;
    children: Snippet<[{ id: string; 'aria-describedby': string | undefined }]>;
  } = $props();

  const uid = $props.id();
  const id = `setting-${uid}`;
  const helpId = $derived(help ? `${id}-help` : undefined);
</script>

<div class="setting">
  <div class="setting__text">
    <label class="setting__label" for={id}>{label}</label>
    {#if meta}<p class="setting__meta"><code>{meta}</code></p>{/if}
    {#if help}<p class="setting__help" id={helpId}>{help}</p>{/if}
  </div>
  <div class="setting__control">{@render children({ id, 'aria-describedby': helpId })}</div>
</div>

<style>
  .setting {
    display: grid;
    grid-template-columns: minmax(13rem, 1fr) minmax(0, 1.4fr);
    gap: var(--space-6);
    padding-block: var(--space-5);
    border-top: var(--rule-hair) solid var(--color-rule);
  }
  .setting__label {
    display: block;
    font-weight: 700;
  }
  .setting__meta {
    margin: var(--space-1) 0 0;
    font-size: var(--step--1);
    color: var(--color-muted);
  }
  .setting__meta code {
    font-family: var(--font-mono, ui-monospace, monospace);
    overflow-wrap: anywhere;
  }
  .setting__help {
    margin: var(--space-1) 0 0;
    font-size: var(--step--1);
    color: var(--color-muted);
  }
  .setting__control {
    display: grid;
    gap: var(--space-3);
    align-content: start;
    min-width: 0;
  }
  @media (max-width: 48rem) {
    .setting {
      grid-template-columns: 1fr;
      gap: var(--space-3);
    }
  }
</style>
