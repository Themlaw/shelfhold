<script lang="ts">
  // The preview of a rename plan (EF-EXP-030, EF-EXP-037): the steps, the batches, what is
  // blocked, and, before Kavita readers lose their progress, the figure and a phrase to type.
  import TypedConfirm from './TypedConfirm.svelte';
  import { t } from '#lib/i18n/index.ts';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';

  type Step = { kind: string; src: string; dst: string };
  type Warning = {
    code: string;
    readers: number;
    exact: boolean;
    series: string[];
    members?: string[];
  };
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
  <p class="muted">{t('plan.nothing')}</p>
{:else}
  <ul role="list" aria-label={t('plan.renames')} class="steps">
    {#each plan.steps as step (`${step.src}\u0000${step.dst}`)}
      <li>{step.src} → {step.dst}</li>
    {/each}
  </ul>
  <p class="num">{t('plan.batches', { count: plan.steps.length, batches: plan.batches })}</p>
  {#if plan.blocked.length > 0}
    <p class="num">{t('plan.blocked', { count: plan.blocked.length })}</p>
  {/if}
  {#if progressWarning}
    <Alert tone="warning" live="assertive" title={warningText(progressWarning)}>
      {#if progressWarning.members && progressWarning.members.length > 0}
        {t('plan.warning_members', { members: progressWarning.members.join(', ') })}
      {/if}
    </Alert>
  {/if}
  {#if hasProgressLoss}
    <TypedConfirm
      phrase={phrase}
      confirmLabel={t('plan.apply')}
      onconfirm={() => onapply({ confirmProgressLoss: true })}
    />
  {:else}
    <div><Button variant="primary" onclick={() => onapply({ confirmProgressLoss: false })}>{t('plan.apply')}</Button></div>
  {/if}
{/if}
{#if oncancel}
  <div><Button variant="ghost" onclick={oncancel}>{t('plan.cancel')}</Button></div>
{/if}

<style>
  .steps {
    list-style: none;
    margin: 0;
    padding: 0;
    border-top: var(--rule-hair) solid var(--color-ink);
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: var(--step--1);
  }
  .steps li {
    padding: var(--space-2) 0;
    border-bottom: var(--rule-hair) solid var(--color-rule);
    overflow-wrap: anywhere;
  }
  p {
    margin: 0;
  }
</style>
