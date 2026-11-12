<script lang="ts">
  import { problemDisplay, type ProblemLike } from '#lib/api/problems.ts';

  let {
    problem = null as ProblemLike | null,
    code = undefined as string | undefined,
    title = undefined as string | undefined,
    text = undefined as string | undefined,
    cause = undefined as string | null | undefined,
    detail = undefined as string | null | undefined,
    action = undefined as string | null | undefined,
    request_id = undefined as string | null | undefined,
    requestId = undefined as string | null | undefined,
    status = undefined as number | undefined
  } = $props();

  const display = $derived(
    problemDisplay({
      problem,
      code,
      title: title ?? text,
      cause: cause ?? detail,
      action,
      request_id: request_id ?? requestId,
      status
    })
  );
</script>

<section role="alert" class="problem-card" aria-labelledby="problem-code">
  {#if display.code}
    <p class="code" id="problem-code">{display.code}</p>
  {/if}
  <p class="title">{display.title}</p>
  {#if display.cause}
    <p class="cause">{display.cause}</p>
  {/if}
  {#if display.action}
    <p class="action">{display.action}</p>
  {/if}
  {#if display.requestId}
    <p class="request-id">{display.requestId}</p>
  {/if}
</section>

<style>
  .problem-card {
    border: 1px solid var(--color-rule);
    border-radius: var(--radius-cover);
    padding: 1rem;
    color: var(--color-err);
  }
  .code {
    font-family: monospace;
    font-weight: 600;
  }
  .title {
    font-weight: 600;
  }
</style>