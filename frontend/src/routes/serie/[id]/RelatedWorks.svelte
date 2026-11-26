<script lang="ts">
  // « Œuvres liées » (EF-BIB-039): the relations of one Work, read from its own side. The server
  // is the only source of what is shown: it has already left out every relation whose other end
  // this reader may not see, and counted only the others. Two Works that are related stay two:
  // nothing here merges, moves an Edition or changes a rating. Making and removing a relation is
  // the admin's (`canEdit`); the server refuses anyone else.
  import { relationsAdd, relationsRead, relationsRemove, search as apiSearch } from '#lib/api/index.ts';
  import type { RelationOut, SearchHitOut } from '#lib/api/types.gen.ts';
  import ProblemAlert from '#lib/ui/molecules/ProblemAlert.svelte';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Input from '#lib/ui/atoms/Input.svelte';
  import Select from '#lib/ui/atoms/Select.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import Field from '#lib/ui/molecules/Field.svelte';

  let { workId, canEdit = false }: { workId: string; canEdit?: boolean } = $props();

  const KINDS = ['sequel_of', 'prequel_of', 'adaptation_of', 'spin_off_of'] as const;
  type Kind = (typeof KINDS)[number];
  const LABELS: Record<Kind, { out: MessageKey; in: MessageKey }> = {
    sequel_of: { out: 'related.kind.sequel_of.out', in: 'related.kind.sequel_of.in' },
    prequel_of: { out: 'related.kind.prequel_of.out', in: 'related.kind.prequel_of.in' },
    adaptation_of: { out: 'related.kind.adaptation_of.out', in: 'related.kind.adaptation_of.in' },
    spin_off_of: { out: 'related.kind.spin_off_of.out', in: 'related.kind.spin_off_of.in' }
  };

  let items = $state<RelationOut[]>([]);
  let problem = $state<ProblemView | null>(null);
  let query = $state('');
  let found = $state<SearchHitOut[]>([]);
  let chosen = $state<SearchHitOut | null>(null);
  let kind = $state<Kind>('sequel_of');

  const headers = () => ({ 'X-CSRF-Token': csrfToken() });

  function label(item: RelationOut): string {
    const known = LABELS[item.kind as Kind];
    return known ? t(item.outgoing ? known.out : known.in) : item.kind;
  }

  async function load(): Promise<void> {
    const { data } = await relationsRead({ path: { work_id: workId } });
    // A Work she may not see answers like an absent one: nothing is drawn, no error either.
    items = data?.items ?? [];
  }

  async function lookup(): Promise<void> {
    chosen = null;
    const q = query.trim();
    if (q.length < 2) {
      found = [];
      return;
    }
    const { data } = await apiSearch({ query: { q, limit: 5, scope: 'local' } });
    // `scope: 'local'` answers the catalogue's own hits; anything else is not a Work to relate
    found = (data?.items ?? []).filter(
      (hit): hit is SearchHitOut => 'work_id' in hit && hit.work_id !== workId
    );
  }

  async function add(): Promise<void> {
    if (!chosen) return;
    const { error } = await relationsAdd({
      headers: headers(),
      path: { work_id: workId },
      body: { to_work_id: chosen.work_id, kind }
    });
    if (error !== undefined) {
      problem = problemView(error);
      return;
    }
    problem = null;
    query = '';
    found = [];
    chosen = null;
    await load();
  }

  async function remove(item: RelationOut): Promise<void> {
    const { error } = await relationsRemove({
      headers: headers(),
      path: {
        // the relation is stored from the Work that is `kind` of the other
        work_id: item.outgoing ? workId : item.work_id,
        kind: item.kind,
        to_work_id: item.outgoing ? item.work_id : workId
      }
    });
    if (error !== undefined) {
      problem = problemView(error);
      return;
    }
    problem = null;
    await load();
  }

  $effect(() => {
    void workId;
    void load();
  });
</script>

{#if items.length > 0 || canEdit}
  <section class="section" aria-labelledby="related-works-title">
    <div class="section-head"><h2 id="related-works-title">{t('related.title')}</h2></div>
    {#if items.length > 0}
      <p class="hint">{t('related.intro')}</p>
      <ul class="entries related">
        {#each items as item (`${item.kind}|${item.outgoing}|${item.work_id}`)}
          <li>
            <span class="folio">{label(item)}</span>
            <span class="related__title">
              {#if item.edition_id}
                <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- an internal route of the app -->
                <a href={`/serie/${item.edition_id}`}>{item.title}</a>
              {:else}
                <strong>{item.title}</strong>
              {/if}
            </span>
            {#if canEdit}
              <Button
                variant="danger-ghost"
                size="sm"
                aria-label={t('related.remove_label', { title: item.title })}
                onclick={() => remove(item)}>{t('related.remove')}</Button
              >
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
    {#if canEdit}
      <div class="add stack">
        <h3>{t('related.add.title')}</h3>
        <Field label={t('related.add.search')}>
          {#snippet children(control)}
            <Input {...control} type="search" bind:value={query} oninput={lookup} />
          {/snippet}
        </Field>
        {#if found.length > 0 && !chosen}
          <ul class="entries found">
            {#each found as hit (hit.work_id)}
              <li>
                <button class="found__pick" type="button" onclick={() => (chosen = hit)}>{hit.title}</button>
              </li>
            {/each}
          </ul>
        {/if}
        {#if chosen}
          <Alert tone="info" live="polite" title={t('related.add.chosen', { title: chosen.title })} />
          <Field label={t('related.add.kind')}>
            {#snippet children(control)}
              <Select
                {...control}
                bind:value={kind}
                options={KINDS.map((k) => ({ value: k, label: t(LABELS[k].out) }))}
              />
            {/snippet}
          </Field>
          <Button variant="primary" onclick={add}>{t('related.add.action')}</Button>
        {/if}
      </div>
    {/if}
    {#if problem}
      <ProblemAlert {problem} />
    {/if}
  </section>
{/if}

<style>
  .related > li {
    display: grid;
    grid-template-columns: minmax(8rem, 12rem) minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--space-3);
    min-height: var(--target);
    padding: var(--space-2) 0;
  }
  .related__title {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .add {
    margin-top: var(--space-6);
    max-width: var(--form-max);
  }
  .add h3 {
    margin: 0;
  }
  .found__pick {
    display: block;
    width: 100%;
    min-height: var(--target);
    padding: var(--space-2) var(--space-3);
    background: none;
    border: 0;
    color: var(--color-accent-text);
    font: inherit;
    text-align: left;
    overflow-wrap: anywhere;
    cursor: pointer;
  }
  .found__pick:hover {
    background: var(--color-surface);
  }
  @media (max-width: 40rem) {
    .related > li {
      grid-template-columns: minmax(0, 1fr) auto;
    }
    .related > li > .folio {
      grid-column: 1 / -1;
    }
  }
</style>
