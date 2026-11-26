<script lang="ts" module>
  export type Stat = {
    label: string;
    value: string;
    /** a short line under the figure; with `tone`, it carries the icon of the tone */
    note?: string;
    tone?: 'ok' | 'warning' | 'danger';
  };
</script>

<script lang="ts">
  // The key figures at the top of a register (the strip of sante.html): a label, a large tabular
  // figure, a line of context. A tone is told by an icon and the words of the note, never by the
  // colour alone.
  import Icon from '../atoms/Icon.svelte';

  let { stats, label }: { stats: readonly Stat[]; label: string } = $props();
</script>

<dl class="stats" aria-label={label}>
  {#each stats as stat (stat.label)}
    <div class="stats__item">
      <dt>{stat.label}</dt>
      <dd class="stats__value">{stat.value}</dd>
      {#if stat.note}
        <dd class="stats__note stats__note--{stat.tone ?? 'none'}">
          {#if stat.tone}<Icon name={stat.tone === 'ok' ? 'check' : 'alert'} size="sm" />{/if}
          {stat.note}
        </dd>
      {/if}
    </div>
  {/each}
</dl>

<style>
  .stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 11rem), 1fr));
    margin: 0 0 var(--space-7);
    border-top: var(--rule-thick) solid var(--color-ink);
    border-bottom: var(--rule-hair) solid var(--color-rule-strong);
  }
  .stats__item {
    padding: var(--space-3) var(--space-4);
    border-inline-start: var(--rule-hair) solid var(--color-rule);
    min-width: 0;
  }
  .stats__item:first-child {
    padding-inline-start: 0;
    border-inline-start: 0;
  }
  dt {
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  dd {
    margin: 0;
  }
  .stats__value {
    font: 500 var(--step-3) / var(--leading-tight, 1.15) var(--font-text);
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }
  .stats__note {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    font-size: var(--step--1);
    color: var(--color-muted);
  }
  .stats__note--ok {
    color: var(--color-ok);
  }
  .stats__note--warning {
    color: var(--color-warn);
  }
  .stats__note--danger {
    color: var(--color-err);
  }
  @media (max-width: 40rem) {
    .stats__item {
      padding-inline-start: var(--space-3);
    }
    .stats__item:nth-child(odd) {
      padding-inline-start: 0;
      border-inline-start: 0;
    }
  }
</style>
