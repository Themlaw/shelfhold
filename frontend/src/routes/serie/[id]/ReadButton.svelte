<script lang="ts">
  // The verb of the sheet (EF-UX-006, D-P1): "Lire" when the kept copy may be read in full,
  // "Lire un extrait" when only an extract may be read here, nothing when there is nothing to
  // read or nothing the profile may see. The server decides which; asking for the manifest only
  // tells it, and writes nothing.
  import { readerPublication } from '#lib/api/index.ts';
  import type { EditionProgressOut } from '#lib/api/types.gen.ts';
  import { t } from '#lib/i18n/index.ts';
  import Button from '#lib/ui/atoms/Button.svelte';

  // `progress` (the sheet gives it): a reading under way names the chapter to resume at, none says
  // « Commencer »; left out, the verb is the plain « Lire ».
  let {
    unitId,
    progress,
    scope = $bindable(null)
  }: { unitId: string | null; progress?: EditionProgressOut | null; scope?: 'full' | 'excerpt' | null } = $props();

  const resuming = $derived(progress?.state === 'in_progress' ? progress : null);
  const target = $derived(resuming?.unit_id ?? unitId);

  // The sheet is kept when one series gives way to another: the answer follows the Unit.
  $effect(() => {
    const id = target;
    scope = null;
    if (!id) return;
    let stale = false;
    void readerPublication({ path: { carrier_type: 'unit', carrier_id: id } }).then(
      ({ data, error }) => {
        if (!stale) scope = error === undefined && data ? data.scope : null;
      }
    );
    return () => {
      stale = true;
    };
  });
</script>

{#if target && scope}
  <!-- the reader is a page of its own, under its own policy (ADR-0055): a full navigation, never the client router -->
  <Button
    variant="primary"
    size="lg"
    icon="read"
    data-sveltekit-reload
    href={scope === 'excerpt' ? `/lire/unit/${target}?scope=excerpt` : `/lire/unit/${target}`}
  >
    {#if scope === 'excerpt'}
      {t('reader.read_excerpt')}
    {:else if resuming}
      {t('sheet.resume', { number: resuming.number })}
    {:else if progress !== undefined}
      {t('sheet.start')}
    {:else}
      {t('reader.read')}
    {/if}
  </Button>
{/if}
