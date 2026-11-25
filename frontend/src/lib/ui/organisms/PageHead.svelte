<script lang="ts" module>
  export type Crumb = { label: string; href?: string };
</script>

<script lang="ts">
  // The head of an operating screen: the running head (the way back, then an optional folio on the
  // right), the one h1, a lede, and the actions of the screen. Same markup as the home page, so
  // that every screen opens the same way.
  import type { Snippet } from 'svelte';

  let {
    title,
    lede,
    crumbs = [],
    folio,
    actions
  }: {
    title: string;
    lede?: string;
    /** the way back: links, then the current place (no href) */
    crumbs?: readonly Crumb[];
    folio?: string;
    actions?: Snippet;
  } = $props();
</script>

{#if crumbs.length > 0 || folio}
  <div class="running-head">
    {#if crumbs.length > 0}
      <nav aria-label={crumbs[0]?.label}>
        {#each crumbs as crumb, index (index)}
          {#if index > 0}<span aria-hidden="true"> › </span>{/if}
          {#if crumb.href}<!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
            <a href={crumb.href}>{crumb.label}</a>{:else}<span aria-current="page">{crumb.label}</span>{/if}
        {/each}
      </nav>
    {/if}
    {#if folio}<span class="running-head__folio">{folio}</span>{/if}
  </div>
{/if}
<div class="page-head">
  <div class="page-head__text">
    <h1 class="page-title">{title}</h1>
    {#if lede}<p class="lede">{lede}</p>{/if}
  </div>
  {#if actions}<div class="page-head__actions">{@render actions()}</div>{/if}
</div>
