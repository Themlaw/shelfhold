<script lang="ts">
  // The sources of a series in the order they are tried (EF-SRC-037). Only the active ones are
  // ranked and can move; an inactive one shows why the server set it aside and has no button. The
  // order is sent whole (every active id) and the server's answer is the order that stays: on a
  // refusal the problem is shown and the server's order is read again.
  import { bindingsOrder } from '#lib/api/index.ts';
  import type { SheetBindingOut } from '#lib/api/types.gen.ts';
    import { formatDateTime, t, type MessageKey } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import IconButton from '#lib/ui/atoms/IconButton.svelte';
  import ProblemAlert from '#lib/ui/molecules/ProblemAlert.svelte';

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

<section class="section" aria-labelledby="bindings-title">
  <div class="section-head"><h2 id="bindings-title">{t('sheet.bindings.title')}</h2></div>
  {#if bindings.length === 0}
    <p class="hint">{t('sheet.bindings.empty')}</p>
  {/if}
  {#if order.length > 0}
    <ol class="entries">
      {#each order as binding, index (binding.id)}
        <li class="row">
          <span class="folio row__rank">{t('sheet.bindings.rank', { rank: index + 1 })}</span>
          <span class="row__text">
            <span class="row__name">{binding.source_key}</span>
            <span class="hint">{checked(binding)}</span>
          </span>
          {#if canReorder}
            <span class="row__moves">
              <IconButton
                icon="down"
                class="move-up"
                label={t('sheet.bindings.up', { source: binding.source_key })}
                aria-disabled={index === 0 || undefined}
                onclick={() => index > 0 && move(index, -1)}
              />
              <IconButton
                icon="down"
                label={t('sheet.bindings.down', { source: binding.source_key })}
                aria-disabled={index === order.length - 1 || undefined}
                onclick={() => index < order.length - 1 && move(index, 1)}
              />
            </span>
          {/if}
        </li>
      {/each}
    </ol>
  {/if}
  {#if problem}
    <ProblemAlert {problem} level={3} />
  {/if}
  {#if inactive.length > 0}
    <h3 class="inactive-title">{t('sheet.bindings.inactive')}</h3>
    <ul class="entries">
      {#each inactive as binding (binding.id)}
        <li class="row">
          <span class="row__text">
            <span class="row__name">{binding.source_key}</span>
          </span>
          {#if binding.disabled_reason}
            <Badge tone="warning" icon="ban">{t(REASONS[binding.disabled_reason])}</Badge>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2) var(--space-4);
    min-height: var(--target);
    padding: var(--space-2) 0;
  }
  .row__rank {
    flex: none;
    min-width: 4.5rem;
  }
  .row__text {
    display: flex;
    flex: 1 1 12rem;
    flex-direction: column;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .row__moves {
    display: inline-flex;
    flex: none;
    gap: var(--space-1);
    margin-left: auto;
  }
  .row :global(.move-up svg) {
    transform: rotate(180deg);
  }
  .row :global(.icon-btn[aria-disabled='true']) {
    opacity: 0.45;
    cursor: not-allowed;
  }
  .inactive-title {
    margin: var(--space-6) 0 var(--space-3);
    font-size: var(--step-1);
  }
</style>
