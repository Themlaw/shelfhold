<script lang="ts">
    type Gap = {
    id?: string;
    number?: string | number;
    track?: string;
    segment?: string;
    number_norm?: string;
  };

  let {
    gaps = [],
    editionId = ''
  }: {
    gaps?: Gap[];
    editionId?: string;
  } = $props();

  let list = $state<Gap[]>([]);

  $effect(() => {
    list = [...(gaps ?? [])];
  });

  async function acknowledge(gap: Gap): Promise<void> {
    try {
      await fetch(`/api/v1/editions/${editionId}/gaps/ack`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          number: String(gap.number ?? gap.number_norm ?? ''),
          track: gap.track ?? 'main',
          segment: gap.segment ?? '1'
        })
      });
      list = list.filter((g) => g !== gap);
    } catch {
      // keep the gap visible
    }
  }
</script>

<section class="gaps">
  <h2>Trous</h2>
  <ul>
    {#each list as gap (gap.id ?? gap.number)}
      <li>
        <span>{gap.track ?? 'main'} {gap.number ?? gap.number_norm ?? ''}</span>
        <button type="button" onclick={() => acknowledge(gap)}>Acquitter</button>
      </li>
    {/each}
  </ul>
</section>
