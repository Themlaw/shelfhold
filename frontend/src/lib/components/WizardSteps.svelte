<script lang="ts">
  // "Étape n sur 5 : nom": where the admin is in the first-run wizard. A rule of five marks, the
  // ones behind are inked, the current one carries the number, the ones ahead are dotted; the
  // sentence under it says it in words (the marks are not the only way to know).
  import { t } from '#lib/i18n/index.ts';
  import { WIZARD_STEPS, WIZARD_TOTAL, type WizardStep } from '#lib/wizard.ts';

  let { current }: { current: WizardStep['id'] } = $props();
  const at = $derived(WIZARD_STEPS.findIndex((s) => s.id === current));
  const name = $derived(WIZARD_STEPS[at]?.nameKey);
</script>

<div class="wizard-steps">
  <ol class="wizard-steps__marks" aria-hidden="true">
    {#each WIZARD_STEPS as step, index (step.id)}
      <li class="mark" class:mark--past={index < at} class:mark--now={index === at}>
        <span class="mark__no folio">{index + 1}</span>
        <span class="mark__name">{t(step.nameKey)}</span>
      </li>
    {/each}
  </ol>
  <p class="steps" aria-live="polite">
    {t('admin.wizard.step', { n: at + 1, total: WIZARD_TOTAL, name: name ? t(name) : '' })}
  </p>
</div>

<style>
  .wizard-steps {
    margin-bottom: var(--space-6);
  }
  .wizard-steps__marks {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: var(--space-2);
    margin: 0 0 var(--space-2);
    padding: 0;
    list-style: none;
  }
  .mark {
    display: grid;
    gap: var(--space-1);
    padding-top: var(--space-2);
    border-top: var(--rule-thick) dotted var(--color-rule-strong);
    color: var(--color-subtle);
  }
  .mark--past {
    border-top: var(--rule-thick) solid var(--color-ink);
    color: var(--color-muted);
  }
  .mark--now {
    border-top: var(--rule-thick) solid var(--color-accent);
    color: var(--color-accent-text);
    font-weight: 600;
  }
  .mark__no {
    font-size: var(--step-1);
    color: inherit;
  }
  .mark__name {
    font-size: var(--step--1);
  }
  .steps {
    margin: 0;
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  @media (max-width: 40rem) {
    .mark__name {
      display: none;
    }
  }
</style>
