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
    gap: var(--space-2);
    border: 0;
    margin: 0;
    padding: 0;
    min-inline-size: 0;
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
    min-block-size: 44px;
    min-inline-size: 44px;
    padding: 0 var(--space-3);
    border: var(--rule-hair) solid var(--color-rule-strong);
    border-radius: var(--radius-sm);
    background: var(--color-surface);
    cursor: pointer;
  }
  label:hover:not(.on) {
    background: var(--color-surface-2);
  }
  label.on {
    border-color: var(--color-ink);
    background: var(--color-ink);
    color: var(--color-bg);
    font-weight: 600;
  }
  input {
    /* hidden by its look, not by display:none: it stays focusable and in the tab order */
    position: absolute;
    inset: 0;
    opacity: 0;
    width: 100%;
    height: 100%;
    margin: 0;
    cursor: pointer;
  }
  label:has(input:focus-visible) {
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }
</style>
