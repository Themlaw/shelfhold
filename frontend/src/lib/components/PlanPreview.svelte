<script lang="ts">
  // The preview of a rename plan (EF-EXP-030, EF-EXP-037): the steps, the batches, what is
  // blocked, and, before Kavita readers lose their progress, the figure and a phrase to type.
  import TypedConfirm from './TypedConfirm.svelte';
  import { t } from '#lib/i18n/index.ts';

  type Step = { kind: string; src: string; dst: string };
  type Warning = { code: string; readers: number; exact: boolean; series: string[] };
  type Blocked = { code: string; src: string };
  type Plan = {
    plan_id: string | null;
    base_version: string;
    steps: Step[];
    batches: number;
    blocked: Blocked[];
    warnings: Warning[];
  };

  let {
    plan,
    onapply,
    oncancel
  }: { plan: Plan; onapply: (result: { confirmProgressLoss: boolean }) => void; oncancel: () => void } = $props();

  const progressWarning = $derived(plan.warnings.find((w) => w.code === 'progress_loss'));
  const hasProgressLoss = $derived(!!progressWarning && (progressWarning.readers ?? 0) > 0);
  const phrase = $derived(t('plan.phrase'));

  function warningText(w: Warning): string {
    const readers = w.readers;
    if (w.exact) {
      return t('plan.warning_exact', { readers });
    }
    return t('plan.warning_at_least', { readers });
  }
</script>

{#if !plan.plan_id && plan.steps.length === 0}
  <p>{t('plan.nothing')}</p>
{:else}
  <ul role="list" aria-label={t('plan.renames')}>
    {#each plan.steps as step (`${step.src}\u0000${step.dst}`)}
      <li>{step.src} → {step.dst}</li>
    {/each}
  </ul>
  <p>{t('plan.batches', { count: plan.steps.length, batches: plan.batches })}</p>
  {#if plan.blocked.length > 0}
    <p>{t('plan.blocked', { count: plan.blocked.length })}</p>
  {/if}
  {#if progressWarning}
    <div role="alert">{warningText(progressWarning)}</div>
  {/if}
  {#if hasProgressLoss}
    <TypedConfirm
      phrase={phrase}
      confirmLabel={t('plan.apply')}
      onconfirm={() => onapply({ confirmProgressLoss: true })}
    />
  {:else}
    <button type="button" onclick={() => onapply({ confirmProgressLoss: false })}>{t('plan.apply')}</button>
  {/if}
{/if}
{#if oncancel}
  <button type="button" onclick={oncancel}>{t('plan.cancel')}</button>
{/if}

<style>
  ul {
    list-style: none;
    padding: 0;
  }
</style>