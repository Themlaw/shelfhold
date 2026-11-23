<script lang="ts">
  // « Lire hors ligne » on the sheet of a series (M5 L16b, EF-LEC-012): the same control the reader
  // offers (M6 L20, D-P7 (b)), for the Unit the sheet reads.
  import OfflineKeep from '#lib/offline/OfflineKeep.svelte';

  let { unitId }: { unitId: string | null } = $props();
</script>

<OfflineKeep carrierType="unit" carrierId={unitId} />
