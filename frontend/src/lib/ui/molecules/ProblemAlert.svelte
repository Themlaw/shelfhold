<script lang="ts" module>
  import { isForbidden, needsSession } from '#lib/triage.ts';

  export type BlockKind = 'forbidden' | 'signed-out';

  /** The refusal of the session or of the role an error answer stands for, or null. */
  export function blockKind(error: unknown): BlockKind | null {
    if (isForbidden(error)) return 'forbidden';
    if (needsSession(error)) return 'signed-out';
    return null;
  }
</script>

<script lang="ts">
  // What the server refused, told by the interface: the problem it wrote (title, cause, action and
  // the request id to report), never its raw body. A refusal of the session (401) or of the role
  // (403) is a block of its own, with its drawing, so it is not read as a fault of the screen.
  import { t } from '#lib/i18n/index.ts';
  import type { ProblemView } from '#lib/problems.ts';
  import Button from '../atoms/Button.svelte';
  import Alert from './Alert.svelte';
  import ErrorState from './ErrorState.svelte';

  let {
    problem,
    kind = null,
    level = 2,
    note = null,
    onretry
  }: {
    problem: ProblemView;
    kind?: BlockKind | null;
    level?: 2 | 3 | 4;
    /** a sentence of the screen, before what the server said */
    note?: string | null;
    /** a way out for a read that can be tried again */
    onretry?: () => void;
  } = $props();
</script>

{#snippet retry()}
  {#if onretry}<Button size="sm" icon="refresh" onclick={onretry}>{t('error.retry')}</Button>{/if}
{/snippet}

{#if kind}
  <ErrorState
    kind={kind}
    headingLevel={2}
    code={t('error.code', { status: kind === 'forbidden' ? 403 : 401 })}
    title={kind === 'forbidden' ? t('error.forbidden.title') : t('error.signed_out.title')}
    incident={problem.requestId}
    actions={onretry ? retry : undefined}
  >
    <p>{kind === 'forbidden' ? t('error.forbidden.body') : t('error.signed_out.body')}</p>
  </ErrorState>
{:else}
  <Alert tone="danger" title={problem.title} live="assertive" headingLevel={level} incident={problem.requestId} actions={onretry ? retry : undefined}>
    {#if note}<p>{note}</p>{/if}
    {#if problem.code}<p class="problem__code"><code>{problem.code}</code></p>{/if}
    {#if problem.cause}<p>{problem.cause}</p>{/if}
    {#if problem.action}<p>{problem.action}</p>{/if}
  </Alert>
{/if}

<style>
  /* the stable code of the problem (EF-UX-003): to be read out in a report */
  .problem__code code {
    font-family: var(--font-mono);
    font-size: var(--step--1);
    color: var(--color-muted);
    overflow-wrap: anywhere;
  }
</style>
