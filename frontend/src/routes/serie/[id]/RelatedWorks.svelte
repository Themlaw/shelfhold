<script lang="ts">
  // « Œuvres liées » (EF-BIB-039): the relations of one Work, read from its own side. The server
  // is the only source of what is shown: it has already left out every relation whose other end
  // this reader may not see, and counted only the others. Two Works that are related stay two:
  // nothing here merges, moves an Edition or changes a rating. Making and removing a relation is
  // the admin's (`canEdit`); the server refuses anyone else.
  import { relationsAdd, relationsRead, relationsRemove, search as apiSearch } from '#lib/api/index.ts';
  import type { RelationOut, SearchHitOut } from '#lib/api/types.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { t, type MessageKey } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

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
  <section aria-labelledby="related-works-title">
    <h2 id="related-works-title">{t('related.title')}</h2>
    {#if items.length > 0}
      <p>{t('related.intro')}</p>
      <ul>
        {#each items as item (`${item.kind}|${item.outgoing}|${item.work_id}`)}
          <li>
            <span>{label(item)}</span>
            {#if item.edition_id}
              <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- an internal route of the app -->
              <a href={`/serie/${item.edition_id}`}>{item.title}</a>
            {:else}
              <strong>{item.title}</strong>
            {/if}
            {#if canEdit}
              <button
                type="button"
                aria-label={t('related.remove_label', { title: item.title })}
                onclick={() => remove(item)}>{t('related.remove')}</button
              >
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
    {#if canEdit}
      <h3>{t('related.add.title')}</h3>
      <label>
        {t('related.add.search')}
        <input type="search" bind:value={query} oninput={lookup} />
      </label>
      {#if found.length > 0 && !chosen}
        <ul>
          {#each found as hit (hit.work_id)}
            <li>
              <button type="button" onclick={() => (chosen = hit)}>{hit.title}</button>
            </li>
          {/each}
        </ul>
      {/if}
      {#if chosen}
        <p role="status">{t('related.add.chosen', { title: chosen.title })}</p>
        <label>
          {t('related.add.kind')}
          <select bind:value={kind}>
            {#each KINDS as k (k)}
              <option value={k}>{t(LABELS[k].out)}</option>
            {/each}
          </select>
        </label>
        <button type="button" onclick={add}>{t('related.add.action')}</button>
      {/if}
    {/if}
    {#if problem}
      <ProblemCard {problem} />
    {/if}
  </section>
{/if}
