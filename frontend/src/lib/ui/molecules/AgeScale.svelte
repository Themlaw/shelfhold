<script lang="ts" module>
  export type AgeStep = { value: number | null; label: string; hint?: string };
</script>
<script lang="ts">
  // The age ceiling of a child, as a rule with marks: each step is a radio with its name; the
  // ones under the chosen ceiling are inked, the ones above are dotted, the chosen one carries the
  // blue bookmark. A ceiling that is not one of the marks (7) gets a mark of its own, so that the
  // rule never lies. `counts` (steps → works) is optional: when the server gives how many works
  // each step shows, the figure is printed under the name.
  import { formatNumber } from '#lib/i18n/index.ts';

  let {
    name,
    legend,
    steps,
    value = $bindable(null),
    counts,
    onchange
  }: {
    name: string;
    legend: string;
    steps: readonly AgeStep[];
    value?: number | null;
    counts?: Readonly<Record<string, number>>;
    onchange?: (value: number | null) => void;
  } = $props();

  const key = (step: AgeStep) => (step.value === null ? 'none' : String(step.value));
  const chosen = $derived(steps.findIndex((step) => step.value === value));
</script>

<fieldset class="scale">
  <legend>{legend}</legend>
  <div class="scale__row">
    {#each steps as step, index (key(step))}
      <label class="step" class:step--under={chosen >= 0 && index <= chosen} class:step--on={index === chosen}>
        <input
          type="radio"
          {name}
          value={key(step)}
          checked={index === chosen}
          onchange={() => {
            value = step.value;
            onchange?.(step.value);
          }}
        />
        <span class="step__name">{step.label}</span>
        {#if step.hint}<span class="step__hint">{step.hint}</span>{/if}
        {#if counts && counts[key(step)] !== undefined}
          <span class="step__count folio">{formatNumber(counts[key(step)])}</span>
        {/if}
        {#if index === chosen}<span class="step__mark" aria-hidden="true"></span>{/if}
      </label>
    {/each}
  </div>
</fieldset>

<style>
  .scale {
    margin: 0;
    padding: 0;
    border: 0;
    min-inline-size: 0;
  }
  legend {
    padding: 0;
    margin-bottom: var(--space-1);
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  .scale__row {
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(0, 1fr);
    border-bottom: var(--rule-hair) solid var(--color-rule-strong);
  }
  .step {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-block-size: 44px;
    padding: var(--space-3) var(--space-2) var(--space-2);
    border-top: var(--rule-thick) dotted var(--color-rule-strong);
    cursor: pointer;
  }
  .step + .step {
    border-inline-start: var(--rule-hair) solid var(--color-rule);
  }
  .step--under {
    border-top: var(--rule-thick) solid var(--color-ink);
  }
  .step--on {
    background: var(--color-accent-soft);
  }
  .step:hover:not(.step--on) {
    background: var(--color-surface-2);
  }
  .step input {
    position: absolute;
    inset: 0;
    margin: 0;
    opacity: 0;
    cursor: pointer;
  }
  .step:has(input:focus-visible) {
    outline: 2px solid var(--color-focus);
    outline-offset: -3px;
  }
  .step__name {
    font: 600 var(--step-1) / 1.1 var(--font-text);
  }
  .step--on .step__name {
    color: var(--color-accent-text);
  }
  .step__hint {
    color: var(--color-muted);
    font-size: var(--step--1);
    line-height: 1.2;
  }
  .step__count {
    font-size: var(--step--1);
  }
  .step__mark {
    position: absolute;
    top: 0;
    right: var(--space-2);
    inline-size: var(--space-3);
    block-size: var(--space-4);
    background: var(--color-accent);
    clip-path: polygon(0 0, 100% 0, 100% 100%, 50% 75%, 0 100%);
  }
  @media (max-width: 40rem) {
    .scale__row {
      grid-auto-flow: row;
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
    .step + .step {
      border-inline-start: 0;
    }
  }
</style>
