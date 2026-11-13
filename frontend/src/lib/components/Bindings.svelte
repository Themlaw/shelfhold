<script lang="ts">
  // The sources of a series in the order they are tried (EF-SRC-037). Only the active ones are
  // ranked and can move; an inactive one shows why the server set it aside and has no button. The
  // order is sent whole (every active id) and the server's answer is the order that stays: on a
  // refusal the problem is shown and the server's order is read again.
  import { bindingsOrder } from '#lib/api/index.ts';
  import type { SheetBindingOut } from '#lib/api/types.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { formatDateTime, t, type MessageKey } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  let {
    bindings,
    editionId,
    canReorder,
    onreload
  }: {
    bindings: SheetBindingOut[];
    editionId: string;
    canReorder: boolean;
    onreload: () => void | Promise<void>;
  } = $props();

  const REASONS: Record<NonNullable<SheetBindingOut['disabled_reason']>, MessageKey> = {
    not_loaded: 'sheet.reason.not_loaded',
    content_failures: 'sheet.reason.content_failures',
    removed_upstream: 'sheet.reason.removed_upstream',
    manual: 'sheet.reason.manual'
  };

  // Writable derived: it follows the server's list, and a move shows at once until it is read again.
  let order = $derived(bindings.filter((b) => b.active));
  let problem = $state<ProblemView | null>(null);
  const inactive = $derived(bindings.filter((b) => !b.active));

  async function move(index: number, delta: -1 | 1): Promise<void> {
    const target = index + delta;
    if (target < 0 || target >= order.length) return;
    const next = [...order];
    const moved = next[index];
    const other = next[target];
    if (!moved || !other) return;
    next[index] = other;
    next[target] = moved;
    order = next;
    const { error } = await bindingsOrder({
      headers: { 'X-CSRF-Token': csrfToken() },
      path: { edition_id: editionId },
      body: { binding_ids: next.map((b) => b.id) }
    });
    problem = error === undefined ? null : problemView(error);
    await onreload();
  }

  const checked = (b: SheetBindingOut): string =>
    b.last_checked
      ? t('sheet.bindings.checked', { date: formatDateTime(b.last_checked) })
      : t('sheet.bindings.never');
</script>

<section aria-labelledby="bindings-title">
  <h2 id="bindings-title">{t('sheet.bindings.title')}</h2>
  <ol>
    {#each order as binding, index (binding.id)}
      <li>
        <span class="rank">{t('sheet.bindings.rank', { rank: index + 1 })}</span>
        <span class="name">{binding.source_key}</span>
        <span class="muted">{checked(binding)}</span>
        {#if canReorder}
          <button
            type="button"
            disabled={index === 0}
            aria-label={t('sheet.bindings.up', { source: binding.source_key })}
            onclick={() => move(index, -1)}>↑</button
          >
          <button
            type="button"
            disabled={index === order.length - 1}
            aria-label={t('sheet.bindings.down', { source: binding.source_key })}
            onclick={() => move(index, 1)}>↓</button
          >
        {/if}
      </li>
    {/each}
  </ol>
  {#if problem}
    <ProblemCard {problem} level={3} />
  {/if}
  {#if inactive.length > 0}
    <h3>{t('sheet.bindings.inactive')}</h3>
    <ul>
      {#each inactive as binding (binding.id)}
        <li>
          <span class="name">{binding.source_key}</span>
          <span class="reason">{binding.disabled_reason ? t(REASONS[binding.disabled_reason]) : ''}</span>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  ol,
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  li {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--color-rule);
  }
  button {
    min-width: 2.75rem;
    min-height: 2.75rem;
  }
  .muted,
  .reason {
    color: var(--color-muted);
  }
</style>
