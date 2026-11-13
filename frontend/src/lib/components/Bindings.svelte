<script lang="ts">
  type Binding = {
    id?: string;
    source_key?: string;
    source_name?: string;
    title?: string;
    priority?: number;
    rank?: number;
    active?: boolean;
    disabled_reason?: string | null;
  };

  let {
    bindings = [],
    editionId = ''
  }: {
    bindings?: Binding[];
    editionId?: string;
  } = $props();

  let order = $state<Binding[]>([]);

  $effect(() => {
    order = [...(bindings ?? [])];
  });

  function move(index: number, delta: number): void {
    const target = index + delta;
    if (target < 0 || target >= order.length) return;
    const copy = [...order];
    const [item] = copy.splice(index, 1);
    copy.splice(target, 0, item);
    order = copy;
  }
</script>

<section class="bindings">
  <h2>Liaisons</h2>
  <ol>
    {#each order as binding, index (binding.id ?? index)}
      <li class:inactive={binding.active === false}>
        <span class="rank">{binding.priority ?? binding.rank ?? index + 1}</span>
        <span class="name">{binding.source_name ?? binding.source_key ?? binding.title ?? ''}</span>
        {#if binding.active === false}
          <span class="reason">{binding.disabled_reason ?? ''}</span>
        {/if}
        <div class="actions">
          <button type="button" onclick={() => move(index, -1)} aria-label="Monter">↑</button>
          <button type="button" onclick={() => move(index, 1)} aria-label="Descendre">↓</button>
        </div>
      </li>
    {/each}
  </ol>
</section>

<style>
  .inactive .reason { color: var(--danger, #b00); }
</style>
