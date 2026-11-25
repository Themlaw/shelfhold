<script lang="ts">
  // EF-UX-003: an error is shown with its stable code, its text, its cause and its action; an
  // answer with no recognised code shows a generic text and the request identifier.
  import type { ProblemView } from '#lib/problems.ts';
  import { t } from '#lib/i18n/index.ts';
  import Alert from '#lib/ui/molecules/Alert.svelte';

  let {
    problem,
    level = 2,
    note = null
  }: { problem: ProblemView; level?: 1 | 2 | 3; note?: string | null } = $props();
  // Alert has no level one: a page that asks for it keeps its own heading, this one is a level two
  const headingLevel = $derived(level === 1 ? 2 : level);
</script>

<Alert tone="danger" title={problem.title} live="assertive" {headingLevel}>
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
</Alert>

<style>
  .problem__note {
    margin: 0 0 var(--space-2);
  }
  .problem__facts {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: var(--space-1) var(--space-4);
    margin: 0;
  }
  dt {
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  dd {
    margin: 0;
    min-width: 0;
  }
  .problem__code {
    font-family: var(--font-mono, ui-monospace, monospace);
    overflow-wrap: anywhere;
  }
  @media (max-width: 40rem) {
    .problem__facts {
      grid-template-columns: minmax(0, 1fr);
      gap: 0;
    }
    dd {
      margin-bottom: var(--space-2);
    }
  }
</style>
