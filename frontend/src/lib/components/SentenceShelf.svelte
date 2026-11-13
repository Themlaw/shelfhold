<script lang="ts">
  // A single pre-created shelf, shown as the sentence the admin reads (EF-UX-007).
  let { name = '', phrase = '', shelf = null } = $props<{
    name?: string;
    phrase?: string;
    shelf?: { name: string; phrase: string } | null;
  }>();
  const n = $derived(shelf?.name ?? name);
  const p = $derived(shelf?.phrase ?? phrase);
</script>

<p class="sentence-shelf" data-shelf={n}>{p}</p>