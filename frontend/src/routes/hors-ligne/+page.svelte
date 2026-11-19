<script lang="ts">
  // The copies kept on this device (M5 L16b, EF-LEC-012): the signed-in profile's, and nobody
  // else's. It opens with no network (the shell is kept by the worker, the list is read from the
  // device). Titles are inserted as text.
  import { onMount } from 'svelte';
  import { formatDate, t } from '#lib/i18n/index.ts';
  import { offlineStore } from '#lib/offline/runtime.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import type { CopyMeta } from '#lib/offline/store.ts';

  let copies = $state<CopyMeta[]>([]);
  let profile = $state<string | null>(null);
  let loaded = $state(false);

  async function load(): Promise<void> {
    try {
      const store = offlineStore();
      profile = await store.activeProfile();
      const token = csrfToken();
      // another session than the one the copies were kept under (another profile signed in elsewhere): nothing is listed
      const foreign = token !== '' && (await store.sessionMatches(token)) === false;
      copies = profile && !foreign ? await store.copies(profile) : [];
    } catch {
      copies = [];
    }
    loaded = true;
  }

  async function remove(copy: CopyMeta): Promise<void> {
    if (profile) await offlineStore().evictCarrier(profile, copy.carrierType, copy.carrierId);
    await load();
  }

  onMount(() => void load());
</script>

<main>
  <h1>{t('offline.library')}</h1>
  <p>{t('offline.lead')}</p>
  {#if loaded && copies.length === 0}
    <p>{t('offline.empty')}</p>
  {:else}
    <ul>
      {#each copies as copy (copy.carrierType + copy.carrierId)}
        <li>
          <span>{copy.title}</span>
          <small>{t('offline.kept_at', { date: formatDate(new Date(copy.keptAt)) })}</small>
          <!-- the reader is a page of its own, under its own policy (ADR-0055): a full navigation -->
          <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
          <a data-sveltekit-reload href={`/lire/${copy.carrierType}/${copy.carrierId}`}>{t('offline.read')}</a>
          <button type="button" onclick={() => remove(copy)}>{t('offline.remove')}</button>
        </li>
      {/each}
    </ul>
  {/if}
</main>

<style>
  main {
    max-width: 42rem;
    margin: 0 auto;
    padding: 1rem;
  }
  li {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    align-items: center;
    min-height: 2.75rem;
  }
  a,
  button {
    min-height: 2.75rem;
    min-width: 2.75rem;
  }
</style>
