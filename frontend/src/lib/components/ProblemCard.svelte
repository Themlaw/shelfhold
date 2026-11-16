<script lang="ts">
  // EF-UX-003: an error is shown with its stable code, its text, its cause and its action; an
  // answer with no recognised code shows a generic text and the request identifier.
  import type { ProblemView } from '#lib/problems.ts';
  import { t } from '#lib/i18n/index.ts';

  let {
    problem,
    level = 2,
    note = null
  }: { problem: ProblemView; level?: 1 | 2 | 3; note?: string | null } = $props();
</script>

<section role="alert" class="problem" aria-labelledby="problem-title">
  <svelte:element this={`h${level}`} class="title" id="problem-title">{problem.title}</svelte:element>
  {#if note}
    <p class="note">{note}</p>
  {/if}
  <dl>
    {#if problem.code}
      <dt>{t('problem.code')}</dt>
      <dd class="code">{problem.code}</dd>
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
      <dd class="code">{problem.requestId}</dd>
    {/if}
  </dl>
</section>

<style>
  .problem {
    border: 1px solid var(--color-rule);
    border-radius: var(--radius-cover);
    padding: 1rem;
  }
  .title {
    margin: 0 0 0.5rem;
    font-size: 1rem;
    font-weight: 600;
    color: var(--color-err);
  }
  .note {
    margin: 0 0 0.5rem;
  }
  dl {
    margin: 0;
  }
  dt {
    color: var(--color-muted);
    font-size: 0.875rem;
  }
  dd {
    margin: 0 0 0.5rem;
  }
  .code {
    font-family: monospace;
    overflow-wrap: anywhere;
  }
</style>
