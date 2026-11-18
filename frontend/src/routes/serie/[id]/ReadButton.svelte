<script lang="ts">
  // The verb of the sheet (EF-UX-006, D-P1): "Lire" when the kept copy may be read in full,
  // "Lire un extrait" when only an extract may be read here, nothing when there is nothing to
  // read or nothing the profile may see. The server decides which; asking for the manifest only
  // tells it, and writes nothing.
  import { onMount } from 'svelte';
  import { readerPublication } from '#lib/api/index.ts';
  import { t } from '#lib/i18n/index.ts';

  let { unitId }: { unitId: string | null } = $props();
  let scope = $state<'full' | 'excerpt' | null>(null);

  onMount(async () => {
    if (!unitId) return;
    const { data, error } = await readerPublication({
      path: { carrier_type: 'unit', carrier_id: unitId }
    });
    scope = error === undefined && data ? data.scope : null;
  });
</script>

{#if unitId && scope}
  <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
  <a class="read" href={scope === 'excerpt' ? `/lire/unit/${unitId}?scope=excerpt` : `/lire/unit/${unitId}`}>
    {scope === 'excerpt' ? t('reader.read_excerpt') : t('reader.read')}
  </a>
{/if}

<style>
  .read {
    display: inline-flex;
    align-items: center;
    min-height: 2.75rem;
    min-width: 2.75rem;
    color: var(--color-accent);
    text-decoration: underline;
  }
</style>
