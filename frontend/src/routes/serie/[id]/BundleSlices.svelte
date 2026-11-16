<script lang="ts">
  // « Bloc pour liseuse » (EF-INT-007, ADR-0053 §1 et §8): the parts of the series block the OPDS
  // feed offers, and the slice, the number of chapters a part holds. The server plans the parts
  // from its manifest and tells what the admin may do (`can`); this screen draws nothing for a
  // reader who is not the admin (403) and sets nothing the server did not allow.
  import { bundleSlicesRead, bundleSlicesSet } from '#lib/api/index.ts';
  import type { BundleOut } from '#lib/api/types.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  let { editionId }: { editionId: string } = $props();

  let loaded = $state<BundleOut | null>(null);
  let problem = $state<ProblemView | null>(null);
  let outOfRange = $state(false);
  let saved = $state(false);
  let value = $state<number | null>(null);

  const left = $derived(loaded ? loaded.excluded.format + loaded.excluded.unsafe : 0);

  function show(next: BundleOut): void {
    loaded = next;
    value = next.slice_chapters;
  }

  async function load(): Promise<void> {
    const { data } = await bundleSlicesRead({ path: { edition_id: editionId } });
    // Not the admin, or no such Edition: nothing is drawn, and no error either.
    if (data) show(data);
    else loaded = null;
  }

  async function save(chapters: number | null): Promise<void> {
    saved = false;
    outOfRange = false;
    if (loaded === null) return;
    if (
      chapters !== null &&
      !(Number.isInteger(chapters) && chapters >= loaded.slice_min && chapters <= loaded.slice_max)
    ) {
      outOfRange = true;
      return;
    }
    const { data, error } = await bundleSlicesSet({
      headers: { 'X-CSRF-Token': csrfToken() },
      path: { edition_id: editionId },
      body: { slice_chapters: chapters }
    });
    if (error !== undefined || !data) {
      problem = problemView(error);
      return;
    }
    problem = null;
    show(data);
    saved = true;
  }

  $effect(() => {
    void editionId;
    saved = false;
    problem = null;
    void load();
  });
</script>

{#if loaded !== null}
  <section aria-labelledby="bundle-slices-title">
    <h2 id="bundle-slices-title">{t('bundleSlices.title')}</h2>
    <p>{t('bundleSlices.intro')}</p>
    <ul>
      {#each loaded.parts as part (part.index)}
        <li>
          {t(
            part.first_number === part.last_number
              ? 'bundleSlices.part.single'
              : 'bundleSlices.part.range',
            {
              index: part.index,
              total: loaded.parts.length,
              first: part.first_number,
              last: part.last_number,
              count: part.chapters
            }
          )}
        </li>
      {/each}
    </ul>
    {#if left > 0}
      <p>{t('bundleSlices.excluded', { count: left })}</p>
    {/if}
    {#if loaded.can.set_slice}
      <form
        onsubmit={(event) => {
          event.preventDefault();
          void save(value);
        }}
      >
        <label>
          {t('bundleSlices.field')}
          <input type="number" inputmode="numeric" step="1" bind:value />
        </label>
        <button type="submit">{t('bundleSlices.save')}</button>
        {#if loaded.slice_chapters !== null}
          <button type="button" onclick={() => save(null)}>{t('bundleSlices.clear')}</button>
        {/if}
      </form>
    {/if}
    {#if outOfRange}
      <p role="alert">
        {t('bundleSlices.range', { min: loaded.slice_min, max: loaded.slice_max })}
      </p>
    {/if}
    {#if saved}
      <p role="status">{t('bundleSlices.saved')}</p>
    {/if}
    {#if problem}
      <ProblemCard {problem} />
    {/if}
  </section>
{/if}
