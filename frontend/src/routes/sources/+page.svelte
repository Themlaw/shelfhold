<script lang="ts">
  // "Sources" (EF-SRC-014): the sources that are not well, each with the way out. The same
  // administrator-only answer as the Health page; a refusal is shown, never an empty list. The
  // address and the settings of a source are not in this answer.
  import { onMount } from 'svelte';
  import { healthPage } from '#lib/api/index.ts';
  import HealthEntry from '#lib/components/HealthEntry.svelte';
  import ProblemAlert from '#lib/ui/molecules/ProblemAlert.svelte';
  import { healthView, type SourceEntry } from '#lib/healthPage.ts';
  import { t } from '#lib/i18n/index.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';

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

<svelte:head>
  <title>{t('sources_page.title')}</title>
</svelte:head>

<main class="page">
  <PageHead
    title={t('sources_page.title')}
    crumbs={[{ label: t('health_page.crumb'), href: '/reglages' }, { label: t('sources_page.title') }]}
    lede={degraded && degraded.length > 0 ? t('sources_page.lede', { count: degraded.length }) : undefined}
  >
    {#snippet actions()}
      {#if degraded && degraded.length > 0}
        <Button href="/sante" variant="secondary" icon="pulse">{t('sources_page.health_link')}</Button>
      {/if}
    {/snippet}
  </PageHead>
  {#if problem}
    <ProblemAlert {problem} />
  {:else if degraded === null}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('sources_page.loading')}</p>
      <Skeleton /><Skeleton /><Skeleton short />
    </div>
  {:else if degraded.length === 0}
    <EmptyState title={t('sources_page.none')}>
      {t('sources_page.none_help')}
      {#snippet actions()}
        <Button href="/sante" variant="secondary">{t('sources_page.health_link')}</Button>
      {/snippet}
    </EmptyState>
  {:else}
    <ul class="entries">
      {#each degraded as source (source.pluginId)}
        <HealthEntry
          label={`${source.pluginId} — ${t(`health_page.state.${source.state}`)}`}
          code={source.code}
          action={source.action}
        />
      {/each}
    </ul>
  {/if}
</main>
