<script lang="ts">
  // "Sources" (EF-SRC-014): the sources that are not well, each with the way out. The same
  // administrator-only answer as the Health page; a refusal is shown, never an empty list. The
  // address and the settings of a source are not in this answer.
  import { onMount } from 'svelte';
  import { healthPage } from '#lib/api/index.ts';
  import EmptyState from '#lib/components/EmptyState.svelte';
  import HealthEntry from '#lib/components/HealthEntry.svelte';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { healthView, type SourceEntry } from '#lib/healthPage.ts';
  import { t } from '#lib/i18n/index.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  let degraded = $state<SourceEntry[] | null>(null);
  let problem = $state<ProblemView | null>(null);

  onMount(async () => {
    const { data, error } = await healthPage();
    if (error !== undefined || data === undefined) {
      problem = problemView(error);
      return;
    }
    degraded = healthView(data).sources.filter((s) => s.state !== 'ok');
  });
</script>

<main>
  <h1>{t('sources_page.title')}</h1>
  {#if problem}
    <ProblemCard {problem} />
  {:else if degraded}
    {#if degraded.length === 0}
      <EmptyState
        title={t('sources_page.none')}
        description={t('sources_page.none_help')}
        actionLabel={t('sources_page.health_link')}
        href="/sante"
      />
    {:else}
      <ul>
        {#each degraded as source (source.pluginId)}
          <HealthEntry
            label={`${source.pluginId} — ${t(`health_page.state.${source.state}`)}`}
            code={source.code}
            action={source.action}
          />
        {/each}
      </ul>
    {/if}
  {/if}
</main>
