<script lang="ts">
  // The numbers the series skips (EF-BIB-056). "Acquitter" says that a number does not exist: the
  // gap leaves the screen only once the server accepted it, and stays with its error otherwise.
  import { gapAck } from '#lib/api/index.ts';
  import type { SheetGapOut } from '#lib/api/types.gen.ts';
    import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import ProblemAlert from '#lib/ui/molecules/ProblemAlert.svelte';

  let { gaps, editionId }: { gaps: SheetGapOut[]; editionId: string } = $props();

  let done = $state<string[]>([]);
  let problem = $state<ProblemView | null>(null);
  const key = (g: SheetGapOut): string => `${g.track}|${g.segment}|${g.number}`;
  const shown = $derived(gaps.filter((g) => !done.includes(key(g))));

  async function acknowledge(gap: SheetGapOut): Promise<void> {
    const { error } = await gapAck({
      headers: { 'X-CSRF-Token': csrfToken() },
      path: { edition_id: editionId },
      body: { track: gap.track, segment: gap.segment, number: gap.number }
    });
    if (error !== undefined) {
      problem = problemView(error);
      return;
    }
    problem = null;
    done = [...done, key(gap)];
  }
</script>

<section class="section" aria-labelledby="gaps-title">
  <div class="section-head"><h2 id="gaps-title">{t('sheet.gaps.title')}</h2></div>
  {#if shown.length === 0}
    <p class="hint">{t('sheet.gaps.none')}</p>
  {:else}
    <ul class="entries">
      {#each shown as gap (key(gap))}
        <li class="row">
          <span class="row__text">{t('sheet.gaps.item', { track: gap.track, number: gap.number })}</span>
          <Button
            variant="secondary"
            size="sm"
            aria-label={t('sheet.gaps.ack_label', { track: gap.track, number: gap.number })}
            onclick={() => acknowledge(gap)}>{t('sheet.gaps.ack')}</Button
          >
        </li>
      {/each}
    </ul>
  {/if}
  {#if problem}
    <ProblemAlert {problem} level={3} />
  {/if}
</section>

<style>
  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2) var(--space-4);
    min-height: var(--target);
    padding: var(--space-2) 0;
  }
  .row__text {
    flex: 1 1 10rem;
    min-width: 0;
    overflow-wrap: anywhere;
  }
</style>
