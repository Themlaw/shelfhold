<script lang="ts">
  // "Étape n sur 5 : nom": where the admin is in the first-run wizard.
  import { t } from '#lib/i18n/index.ts';
  import { WIZARD_STEPS, WIZARD_TOTAL, type WizardStep } from '#lib/wizard.ts';

  let { current }: { current: WizardStep['id'] } = $props();
  const at = $derived(WIZARD_STEPS.findIndex((s) => s.id === current));
  const name = $derived(WIZARD_STEPS[at]?.nameKey);
</script>

<p class="steps" aria-live="polite">
  {t('admin.wizard.step', { n: at + 1, total: WIZARD_TOTAL, name: name ? t(name) : '' })}
</p>
