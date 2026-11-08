<script lang="ts">
  // The "À ranger" view (EF-EXP-046): the Editions on no shelf. They are not exported; their
  // copy is kept as received. Read only: choosing a shelf is not part of this screen.
  import { onMount } from 'svelte';
  import { toShelveList } from '#lib/api/index.ts';
  import type { ToShelveItem } from '#lib/api/types.gen.ts';
  import { t } from '#lib/i18n/index.ts';
  import { needsSession } from '#lib/triage.ts';

  let items = $state<ToShelveItem[]>([]);
  let loaded = $state(false);
  let signedOut = $state(false);
  let failed = $state(false);

  onMount(async () => {
    try {
      const { data } = await toShelveList({ throwOnError: true });
      items = data.items;
      loaded = true;
    } catch (error) {
      if (needsSession(error)) signedOut = true;
      else failed = true;
    }
  });
</script>

<svelte:head>
  <title>{t('toShelve.title')}</title>
</svelte:head>

<main>
  <h1>{t('toShelve.title')}</h1>
  <p class="muted">{t('toShelve.explain')}</p>

  {#if signedOut}
    <p role="alert" class="err">{t('toRate.signed_out')}</p>
  {:else if failed}
    <p role="alert" class="err">{t('toShelve.error')}</p>
  {:else if loaded && items.length === 0}
    <p>{t('toShelve.none')}</p>
  {:else if items.length > 0}
    <ul>
      {#each items as item (item.edition_id)}
        <li>
          <strong>{item.title}</strong>
          <span class="muted">{item.lang} · {item.media}</span>
        </li>
      {/each}
    </ul>
  {/if}
</main>

<style>
  main {
    max-width: 42rem;
    margin: 0 auto;
    padding: 3rem 1rem;
  }
  h1 {
    font-family: var(--font-display);
    font-weight: 400;
    font-style: italic;
    letter-spacing: -0.02em;
  }
  ul {
    list-style: none;
    padding: 0;
    display: grid;
    gap: 0.75rem;
  }
  li {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem 1rem;
    border: 1px solid var(--color-rule);
    border-radius: var(--radius-cover);
    background: var(--color-surface);
    overflow-wrap: anywhere;
  }
  .muted {
    color: var(--color-muted);
  }
  .err {
    color: var(--color-err);
  }
</style>
