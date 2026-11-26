<script lang="ts">
  // « Bloc pour liseuse » (EF-INT-007, ADR-0053 §1 et §8): the parts of the series block the OPDS
  // feed offers, and the slice, the number of chapters a part holds. The server plans the parts
  // from its manifest and tells what the admin may do (`can`); this screen draws nothing for a
  // reader who is not the admin (403) and sets nothing the server did not allow.
  import { bundleSlicesRead, bundleSlicesSet } from '#lib/api/index.ts';
  import type { BundleOut } from '#lib/api/types.gen.ts';
  import ProblemAlert from '#lib/ui/molecules/ProblemAlert.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';

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
  <section class="section" aria-labelledby="bundle-slices-title">
    <div class="section-head"><h2 id="bundle-slices-title">{t('bundleSlices.title')}</h2></div>
    <p class="prose">{t('bundleSlices.intro')}</p>
    <ol class="entries parts">
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
    </ol>
    {#if left > 0}
      <p class="hint">{t('bundleSlices.excluded', { count: left })}</p>
    {/if}
    {#if loaded.can.set_slice}
      <form
        class="slice-form"
        onsubmit={(event) => {
          event.preventDefault();
          void save(value);
        }}
      >
        <Field label={t('bundleSlices.field')}>
          {#snippet children(control)}
            <Input {...control} type="number" inputmode="numeric" step="1" width="short" bind:value />
          {/snippet}
        </Field>
        <div class="btn-row">
          <Button type="submit" variant="primary">{t('bundleSlices.save')}</Button>
          {#if loaded.slice_chapters !== null}
            <Button variant="ghost" onclick={() => save(null)}>{t('bundleSlices.clear')}</Button>
          {/if}
        </div>
      </form>
    {/if}
    {#if outOfRange}
      <Alert
        tone="warning"
        live="assertive"
        title={t('bundleSlices.range', { min: loaded.slice_min, max: loaded.slice_max })}
      />
    {/if}
    {#if saved}
      <Alert tone="success" live="polite" title={t('bundleSlices.saved')} />
    {/if}
    {#if problem}
      <ProblemAlert {problem} />
    {/if}
  </section>
{/if}

<style>
  .parts {
    margin-block: var(--space-4);
  }
  .parts > li {
    padding: var(--space-2) 0;
    overflow-wrap: anywhere;
  }
  .slice-form {
    display: grid;
    gap: var(--space-4);
    margin-block: var(--space-4);
  }
</style>
