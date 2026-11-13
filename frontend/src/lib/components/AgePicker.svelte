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
    gap: 0.25rem 0.5rem;
    border: 0;
    margin: 0;
    padding: 0;
  }
  legend {
    padding: 0;
    color: var(--color-muted);
    font-size: 0.875rem;
  }
  label {
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    min-height: 2.75rem;
    min-width: 2.75rem;
    padding: 0 0.625rem;
    border: 1px solid var(--color-rule);
    border-radius: var(--radius-cover);
    background: var(--color-surface);
    cursor: pointer;
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
    outline: 2px solid var(--color-accent);
    outline-offset: 2px;
  }
</style>
