<script lang="ts">
  // EF-UX-003: an error is shown with its stable code, its text, its cause and its action; an
  // answer with no recognised code shows a generic text and the request identifier.
  import type { ProblemView } from '#lib/problems.ts';
  import { t } from '#lib/i18n/index.ts';
  import Icon from '#lib/ui/atoms/Icon.svelte';

  let {
    problem,
    level = 2,
    note = null
  }: { problem: ProblemView; level?: 1 | 2 | 3; note?: string | null } = $props();

  const uid = $props.id();
  const titleId = $derived(`problem-title-${uid}`);
</script>

<section role="alert" class="problem" aria-labelledby={titleId}>
  <Icon name="alert" />
  <svelte:element this={`h${level}`} class="problem__title" id={titleId}>{problem.title}</svelte:element>
  <div class="problem__body">
    {#if note}
      <p class="problem__note">{note}</p>
    {/if}
    <dl class="problem__facts">
      {#if problem.code}
        <dt>{t('problem.code')}</dt>
        <dd class="problem__code">{problem.code}</dd>
      {/if}
      {#if problem.cause}
        <dt>{t('problem.cause')}</dt>
        <dd>{problem.cause}</dd>
      {/if}
      {#if problem.action}
        <dt>{t('problem.action')}</dt>
        <dd>{problem.action}</dd>
      {/if}
      {#if problem.requestId}
        <dt>{t('problem.request_id')}</dt>
        <dd class="problem__code">{problem.requestId}</dd>
      {/if}
    </dl>
  </div>
</section>

<style>
  .problem {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: var(--space-1) var(--space-3);
    align-items: start;
    margin-block: var(--space-4);
    padding: var(--space-3) var(--space-4);
    border: var(--rule-hair) solid var(--color-err);
    border-left-width: var(--rule-thick);
    border-radius: var(--radius-xs);
    background: var(--color-err-soft);
    color: var(--color-ink);
  }
  .problem > :global(.icon) {
    margin-top: 0.1rem;
    color: var(--color-err);
  }
  .problem__title {
    margin: 0;
    font: 700 var(--step-0) / var(--leading-ui) var(--font-ui);
    color: var(--color-err);
  }
  .problem__body {
    grid-column: 2;
  }
  .problem__note {
    margin: 0 0 var(--space-2);
  }
  .problem__facts {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: var(--space-1) var(--space-4);
    margin: 0;
  }
  .problem__facts dt {
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  .problem__facts dd {
    margin: 0;
  }
  .problem__code {
    font-family: var(--font-mono);
    font-size: var(--step--1);
    overflow-wrap: anywhere;
  }
  @media (max-width: 40rem) {
    .problem__facts {
      grid-template-columns: minmax(0, 1fr);
      gap: 0;
    }
    .problem__facts dd {
      margin-bottom: var(--space-2);
    }
  }
</style>
