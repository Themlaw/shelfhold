<script lang="ts">
  // One shelf, as the sentence the admin reads (EF-UX-007). The text is the server's, shown as
  // text, never as markup.
  let { shelf }: { shelf: { name: string; phrase: string } } = $props();
</script>

<p class="sentence-shelf" data-shelf={shelf.name}>{shelf.phrase}</p>
