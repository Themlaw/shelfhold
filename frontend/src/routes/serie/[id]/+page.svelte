<script lang="ts">
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import { editionsList, fieldLock, followCheck, worksSeparate, worksAttachSeries } from '#lib/api/index.ts';
  import Bindings from '#lib/components/Bindings.svelte';
  import Gaps from '#lib/components/Gaps.svelte';
  import CoverGrid from '#lib/components/CoverGrid.svelte';

  let edition = $state<any>(null);
  let bindings = $state<any[]>([]);
  let gaps = $state<any[]>([]);
  let message = $state('');
  let field = $state('title');
  let value = $state('');
  let mergeId = $state('');

  const id = $derived($page.params.id);

  onMount(async () => {
    try {
      const { data } = await editionsList({ throwOnError: true });
      edition = (data.items ?? []).find((e: any) => e.edition_id === id) ?? null;
    } catch {
      edition = null;
    }
  });

  async function checkNow(): Promise<void> {
    try {
      const { data } = await followCheck({ path: { edition_id: id }, throwOnError: true });
      message = data.message ?? 'Vérifié';
    } catch {
      message = 'Erreur';
    }
  }

  async function lockField(): Promise<void> {
    try {
      await fieldLock({
        path: { edition_id: id, field },
        body: { value: value || null },
        throwOnError: true
      });
      message = 'Champ verrouillé';
    } catch {
      message = 'Erreur';
    }
  }

  async function separate(): Promise<void> {
    if (!mergeId) return;
    try {
      await worksSeparate({ path: { merge_id: mergeId }, throwOnError: true });
      message = 'Séparé';
    } catch {
      message = 'Erreur';
    }
  }

  async function attachConfirm(): Promise<void> {
    try {
      await worksAttachSeries({
        path: { work_id: edition?.work_id ?? id },
        body: { series_id: id, position: '1' },
        throwOnError: true
      });
      message = 'Rattaché';
    } catch {
      message = 'Erreur';
    }
  }
</script>

<section>
  <h1>Fiche série</h1>
  <p>{edition?.title ?? id}</p>
  <button type="button" onclick={checkNow}>Vérifier maintenant</button>
  {#if message}<p>{message}</p>{/if}

  <Bindings {bindings} editionId={id} />
  <Gaps {gaps} editionId={id} />

  <section class="edit">
    <h2>Édition manuelle</h2>
    <input bind:value={field} placeholder="champ" />
    <input bind:value={value} placeholder="valeur" />
    <button type="button" onclick={lockField}>Verrouiller</button>
  </section>

  <section class="team">
    <h2>Équipe préférée</h2>
    <select>
      <option value="">Aucune</option>
    </select>
  </section>

  <section class="separate">
    <h2>Séparer</h2>
    <input bind:value={mergeId} placeholder="merge id" />
    <button type="button" onclick={separate}>Séparer</button>
  </section>

  <section class="attach">
    <h2>Rattachement</h2>
    <button type="button" onclick={attachConfirm}>Confirmer l'attachement</button>
  </section>

  <section class="filters">
    <h2>Filtres</h2>
    <label>Étagère <input /></label>
    <label>Série <input /></label>
  </section>

  <section class="status">
    <h2>Statut amont</h2>
    <select>
      <option value="active">actif</option>
      <option value="paused">en pause</option>
      <option value="finished">terminé</option>
    </select>
  </section>
</section>
