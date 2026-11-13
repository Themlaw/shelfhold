<script lang="ts">
  // The numbers the series skips (EF-BIB-056). "Acquitter" says that a number does not exist: the
  // gap leaves the screen only once the server accepted it, and stays with its error otherwise.
  import { gapAck } from '#lib/api/index.ts';
  import type { SheetGapOut } from '#lib/api/types.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

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

<section aria-labelledby="gaps-title">
  <h2 id="gaps-title">{t('sheet.gaps.title')}</h2>
  {#if shown.length === 0}
    <p>{t('sheet.gaps.none')}</p>
  {:else}
    <ul>
      {#each shown as gap (key(gap))}
        <li>
          <span>{t('sheet.gaps.item', { track: gap.track, number: gap.number })}</span>
          <button
            type="button"
            aria-label={t('sheet.gaps.ack_label', { track: gap.track, number: gap.number })}
            onclick={() => acknowledge(gap)}>{t('sheet.gaps.ack')}</button
          >
        </li>
      {/each}
    </ul>
  {/if}
  {#if problem}
    <ProblemCard {problem} level={3} />
  {/if}
</section>

<style>
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  li {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    padding: 0.25rem 0;
  }
  button {
    min-height: 2.75rem;
  }
</style>
