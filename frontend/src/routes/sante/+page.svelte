<script lang="ts">
  // "Santé" (ENF-OBS-005, EF-INT-025): whether the instance is well, for the administrator alone. The
  // server refuses anyone else (403, shown, never an empty page). It reads and applies nothing: each
  // entry shows its stable code and the command that leaves it. No secret, host or path is in it.
  import { onMount } from 'svelte';
  import { healthPage } from '#lib/api/index.ts';
  import HealthEntry from '#lib/components/HealthEntry.svelte';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { healthView, type HealthView } from '#lib/healthPage.ts';
  import { t } from '#lib/i18n/index.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';

  let view = $state<HealthView | null>(null);
  let problem = $state<ProblemView | null>(null);

  onMount(async () => {
    const { data, error } = await healthPage();
    if (error !== undefined || data === undefined) {
      problem = problemView(error);
      return;
    }
    view = healthView(data);
  });
</script>

<main>
  <h1>{t('health_page.title')}</h1>
  {#if problem}
    <ProblemCard {problem} />
  {:else if view}
    <section aria-labelledby="h-sources">
      <h2 id="h-sources">{t('health_page.sources')}</h2>
      <ul>
        {#each view.sources as source (source.pluginId)}
          <HealthEntry
            label={`${source.pluginId} — ${t(`health_page.state.${source.state}`)}`}
            code={source.code}
            action={source.action}
          />
        {/each}
      </ul>
      <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
      <p><a href="/sources">{t('health_page.sources_link')}</a></p>
    </section>

    <section aria-labelledby="h-irreplaceable">
      <h2 id="h-irreplaceable">{t('health_page.irreplaceable')}</h2>
      <p>
        {t('health_page.irreplaceable_counts', {
          count: view.irreplaceable.count,
          bare: view.irreplaceable.withoutKeptCopy,
          single: view.irreplaceable.singleCopy
        })}
      </p>
      <p>
        {#if view.irreplaceable.backupAgeDays === null}
          {t('health_page.store_backup_none')}
        {:else}
          {t('health_page.store_backup_age', { days: view.irreplaceable.backupAgeDays })}
        {/if}
      </p>
      {#if view.lostOriginals.length > 0}
        <h3>{t('health_page.lost_originals')}</h3>
        <ul>
          {#each view.lostOriginals as entry, i (i)}
            <HealthEntry label={entry.subject ?? ''} code={entry.code} action={entry.action} />
          {/each}
        </ul>
      {/if}
    </section>

    <section aria-labelledby="h-backup">
      <h2 id="h-backup">{t('health_page.backup')}</h2>
      <p>
        {view.backup.lastOkAt
          ? t('health_page.backup_last', { at: view.backup.lastOkAt })
          : t('health_page.backup_never')}
      </p>
      <ul>
        {#each view.backup.warnings as entry (entry.code)}
          <HealthEntry label="" code={entry.code} action={entry.action} />
        {/each}
      </ul>
    </section>

    <section aria-labelledby="h-kavita">
      <h2 id="h-kavita">{t('health_page.kavita_audit')}</h2>
      {#if view.kavitaAudit.length === 0}
        <p>{t('health_page.kavita_audit_none')}</p>
      {:else}
        <ul>
          {#each view.kavitaAudit as entry, i (i)}
            <HealthEntry label={entry.subject ?? ''} code={entry.code} action={entry.action} />
          {/each}
        </ul>
      {/if}
    </section>

    <section aria-labelledby="h-types">
      <h2 id="h-types">{t('health_page.library_types')}</h2>
      {#if view.libraryTypeChanges.length === 0}
        <p>{t('health_page.library_types_none')}</p>
      {:else}
        <ul>
          {#each view.libraryTypeChanges as entry, i (i)}
            <HealthEntry label={entry.subject ?? ''} code={entry.code} action={entry.action} />
          {/each}
        </ul>
      {/if}
    </section>

    {#if view.withdrawalPending.count > 0 || (view.jurisdiction && !view.jurisdiction.confirmed)}
      <section aria-labelledby="h-waits">
        <h2 id="h-waits">{t('health_page.waits')}</h2>
        <ul>
          {#if view.withdrawalPending.count > 0}
            <HealthEntry
              label={t('health_page.withdrawal', { count: view.withdrawalPending.count })}
              code="withdrawal_pending"
              action={view.withdrawalPending.action}
            />
          {/if}
          {#if view.jurisdiction && !view.jurisdiction.confirmed}
            <HealthEntry
              label={t('health_page.jurisdiction', { country: view.jurisdiction.country })}
              code="rights.jurisdiction_unconfirmed"
              action={view.jurisdiction.action}
            />
          {/if}
        </ul>
      </section>
    {/if}

    <section aria-labelledby="h-stats">
      <h2 id="h-stats">{t('health_page.stats')}</h2>
      <dl>
        <dt>{t('health_page.stats_waiting')}</dt>
        <dd>{view.stats.waitingStates}</dd>
        <dt>{t('health_page.stats_exported')}</dt>
        <dd>{view.stats.unitsExported}</dd>
        <dt>{t('health_page.stats_arrivals')}</dt>
        <dd>{view.stats.arrivalsPending}</dd>
      </dl>
    </section>

    <section aria-labelledby="h-diagnostic">
      <h2 id="h-diagnostic">{t('health_page.diagnostic')}</h2>
      <p>{t('health_page.diagnostic_help')}</p>
      <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- a file download, not a route -->
      <a href="/api/v1/diagnostic/bundle" download="diagnostic-bundle.zip">
        {t('health_page.diagnostic_download')}
      </a>
    </section>
  {/if}
</main>
