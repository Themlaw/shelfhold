<script lang="ts">
  // « Lectures marquées » (EF-LEC-014, M6 L18): the read marks on the Units of this Edition — the
  // profile's own, and for an administrator the child profiles' — each with the verb that takes
  // the mark back. The undo is a dated line of history: nothing is deleted, the server says how
  // many marks were undone, and this screen draws nothing the server did not answer.
  import { me, readerReadMarks, readerUndoReadMark, readerUndoReadMarkForChild } from '#lib/api/index.ts';
  import type { ReadMarkOut } from '#lib/api/types.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';

  let { editionId }: { editionId: string } = $props();

  let mine = $state('');
  let marks = $state<ReadMarkOut[] | null>(null);
  let problem = $state<ProblemView | null>(null);
  let undone = $state<number | null>(null);

  async function load(): Promise<void> {
    const [who, list] = await Promise.all([
      me(),
      readerReadMarks({ query: { edition_id: editionId } })
    ]);
    // No session, or no such Edition: nothing is drawn, and no error either.
    if (!who.data || !list.data) {
      marks = null;
      return;
    }
    mine = who.data.id;
    marks = list.data.items;
  }

  async function undo(mark: ReadMarkOut): Promise<void> {
    undone = null;
    const headers = { 'X-CSRF-Token': csrfToken() };
    // The own door for the profile's own mark; the administrator's door for a child's.
    const answer =
      mark.user_id === mine
        ? await readerUndoReadMark({ headers, path: { unit_id: mark.unit_id } })
        : await readerUndoReadMarkForChild({
            headers,
            path: { unit_id: mark.unit_id, user_id: mark.user_id }
          });
    if (answer.error !== undefined || !answer.data) {
      problem = problemView(answer.error);
      return;
    }
    problem = null;
    undone = answer.data.undone;
    // The server says how many marks were undone: only a mark really undone leaves the list (a
    // mark already taken back, or not the profile's to undo, stays where it is).
    if (undone > 0 && marks) {
      marks = marks.filter((m) => m.user_id !== mark.user_id || m.unit_id !== mark.unit_id);
    }
  }

  $effect(() => {
    void editionId;
    undone = null;
    problem = null;
    void load();
  });
</script>

{#if marks !== null}
  <section class="section" aria-labelledby="read-marks-title">
    <div class="section-head"><h2 id="read-marks-title">{t('readMarks.title')}</h2></div>
    <p class="hint">{t('readMarks.intro')}</p>
    {#if marks.length === 0}
      <p>{t('readMarks.empty')}</p>
    {:else}
      <ul class="entries marks">
        {#each marks as mark (mark.user_id + '|' + mark.unit_id)}
          <li>
            <span class="marks__text">
              {mark.title
                ? t('readMarks.mark', { number: mark.number, title: mark.title, username: mark.username })
                : t('readMarks.mark_no_title', { number: mark.number, username: mark.username })}
            </span>
            <Button variant="secondary" size="sm" onclick={() => undo(mark)}>{t('readMarks.undo')}</Button>
          </li>
        {/each}
      </ul>
    {/if}
    {#if undone !== null}
      <Alert tone="info" live="polite" title={t('readMarks.undone', { count: undone })} />
    {/if}
    {#if problem}
      <ProblemCard {problem} />
    {/if}
  </section>
{/if}

<style>
  .marks > li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    min-height: var(--target);
    padding: var(--space-2) 0;
  }
  .marks__text {
    flex: 1 1 14rem;
    min-width: 0;
    overflow-wrap: anywhere;
  }
</style>
