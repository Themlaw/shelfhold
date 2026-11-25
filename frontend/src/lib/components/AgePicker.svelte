<script lang="ts">
  // The age selector: one native radio group, so Tab enters it once, the arrow keys move inside
  // it and every choice has a visible label (EF-USR-015). Colours come from CSS tokens only.
  import { t } from '#lib/i18n/index.ts';
  import { AGES, ageLabel, type Age } from '#lib/triage.ts';

  let {
    name,
    legend,
    value = $bindable()
  }: { name: string; legend: string; value?: Age | null | undefined } = $props();
</script>

<fieldset class="picker">
  <legend>{legend}</legend>
  {#each AGES as age (age)}
    <label class:on={value === age}>
      <input type="radio" {name} value={age} bind:group={value} />
      <span>{t(ageLabel(age))}</span>
    </label>
  {/each}
</fieldset>

<style>
  .picker {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1) var(--space-2);
    border: 0;
    margin: 0;
    padding: 0;
    min-width: 0;
  }
  legend {
    padding: 0;
    margin-bottom: var(--space-1);
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  label {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 2.75rem;
    min-width: 2.75rem;
    padding: 0 var(--space-3);
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-radius: var(--radius-sm);
    background: var(--color-surface);
    font-variant-numeric: tabular-nums;
    cursor: pointer;
  }
  label:hover {
    border-color: var(--color-ink);
  }
  label.on {
    border-color: var(--color-accent);
    background: var(--color-accent);
    color: var(--color-on-accent);
  }
  input {
    /* hidden by its look, not by display:none: it stays focusable and in the tab order */
    position: absolute;
    inset: 0;
    opacity: 0;
    width: 100%;
    height: 100%;
    margin: 0;
  }
  label:has(input:focus-visible) {
    outline: var(--rule-thick) solid var(--color-focus);
    outline-offset: 2px;
  }
</style>
