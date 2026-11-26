<script lang="ts">
  // "Santé" (ENF-OBS-005, EF-INT-025): whether the instance is well, for the administrator alone. The
  // server refuses anyone else (403, shown, never an empty page). It reads and applies nothing: each
  // entry shows its stable code and the command that leaves it. No secret, host or path is in it.
  import { onMount } from 'svelte';
  import { healthPage } from '#lib/api/index.ts';
  import HealthEntry from '#lib/components/HealthEntry.svelte';
  import ProblemAlert, { refusalOf } from '#lib/ui/molecules/ProblemAlert.svelte';
  import { healthView, type HealthView } from '#lib/healthPage.ts';
  import { formatDate, formatNumber, t } from '#lib/i18n/index.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Progress from '#lib/ui/atoms/Progress.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import PageHead from '#lib/ui/organisms/PageHead.svelte';
  import StatBar, { type Stat } from '#lib/ui/organisms/StatBar.svelte';

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

  // what asks for the administrator, counted from the entries the server gave
  const attention = $derived(
    view
      ? view.sources.filter((s) => s.state !== 'ok').length +
          view.lostOriginals.length +
          view.backup.warnings.length +
          view.kavitaAudit.length +
          view.unreadableWebhooks.length +
          view.libraryTypeChanges.length +
          view.caches.filter((c) => c.over).length +
          (view.withdrawalPending.count > 0 ? 1 : 0) +
          (view.jurisdiction && !view.jurisdiction.confirmed ? 1 : 0)
      : 0
  );

  const figures = $derived.by<Stat[]>(() => {
    if (!view) return [];
    const bad = view.sources.filter((s) => s.state !== 'ok').length;
    const backupBad = view.backup.warnings.length > 0;
    return [
      {
        label: t('health_page.stat_sources'),
        value: t('health_page.stat_sources_value', { ok: view.sources.length - bad, total: view.sources.length }),
        note: bad > 0 ? t('health_page.stat_sources_bad', { count: bad }) : t('health_page.stat_sources_good'),
        tone: bad > 0 ? 'warning' : 'ok'
      },
      {
        label: t('health_page.stat_bare'),
        value: formatNumber(view.irreplaceable.withoutKeptCopy),
        note: t('health_page.stat_bare_note', { count: view.irreplaceable.count }),
        tone: view.irreplaceable.withoutKeptCopy > 0 ? 'warning' : undefined
      },
      {
        label: t('health_page.stat_backup'),
        value: view.backup.lastOkAt
          ? formatDate(new Date(view.backup.lastOkAt), { day: 'numeric', month: 'short' })
          : t('health_page.stat_backup_never'),
        note: view.backup.lastOkAt
          ? t(backupBad ? 'health_page.stat_backup_warn' : 'health_page.stat_backup_good')
          : undefined,
        tone: view.backup.lastOkAt ? (backupBad ? 'warning' : 'ok') : 'warning'
      },
      {
        label: t('health_page.stat_waiting'),
        value: formatNumber(view.stats.waitingStates),
        note: t('health_page.stat_waiting_note')
      }
    ];
  });
</script>

<svelte:head>
  <title>{t('health_page.title')}</title>
</svelte:head>

<main class="page health">
  <PageHead
    title={t('health_page.title')}
    crumbs={[{ label: t('health_page.crumb'), href: '/reglages' }, { label: t('health_page.title') }]}
    lede={view ? (attention > 0 ? t('health_page.lede_attention', { count: attention }) : t('health_page.lede_ok')) : undefined}
  />
  {#if problem}
    <ProblemAlert {problem} kind={refusalOf(problem)} />
  {:else if !view}
    <div aria-busy="true">
      <p class="visually-hidden" role="status">{t('health_page.loading')}</p>
      <Skeleton shape="block" /><Skeleton /><Skeleton short />
    </div>
  {:else}
    <StatBar stats={figures} label={t('health_page.figures')} />
    <section class="section" aria-labelledby="h-sources">
      <div class="section-head"><h2 id="h-sources">{t('health_page.sources')}</h2></div>
      <ul class="entries">
        {#each view.sources as source (source.pluginId)}
          <HealthEntry
            label={`${source.pluginId} — ${t(`health_page.state.${source.state}`)}`}
            code={source.code}
            action={source.action}
          />
        {/each}
      </ul>
      <p class="more"><Button href="/sources" variant="ghost" size="sm" iconEnd="right">{t('health_page.sources_link')}</Button></p>
    </section>

    <section class="section" aria-labelledby="h-irreplaceable">
      <div class="section-head"><h2 id="h-irreplaceable">{t('health_page.irreplaceable')}</h2></div>
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
        <h3 class="sub">{t('health_page.lost_originals')}</h3>
        <ul class="entries">
          {#each view.lostOriginals as entry, i (i)}
            <HealthEntry label={entry.subject ?? ''} code={entry.code} action={entry.action} />
          {/each}
        </ul>
      {/if}
    </section>

    <section class="section" aria-labelledby="h-backup">
      <div class="section-head"><h2 id="h-backup">{t('health_page.backup')}</h2></div>
      <p>
        {view.backup.lastOkAt
          ? t('health_page.backup_last', { at: view.backup.lastOkAt })
          : t('health_page.backup_never')}
      </p>
      <ul class="entries">
        {#each view.backup.warnings as entry (entry.code)}
          <HealthEntry label="" code={entry.code} action={entry.action} />
        {/each}
      </ul>
    </section>

    <section class="section" aria-labelledby="h-kavita">
      <div class="section-head"><h2 id="h-kavita">{t('health_page.kavita_audit')}</h2></div>
      {#if view.kavitaAudit.length === 0}
        <p class="muted">{t('health_page.kavita_audit_none')}</p>
      {:else}
        <ul class="entries">
          {#each view.kavitaAudit as entry, i (i)}
            <HealthEntry label={entry.subject ?? ''} code={entry.code} action={entry.action} />
          {/each}
        </ul>
      {/if}
    </section>

    {#if view.unreadableWebhooks.length > 0}
      <section class="section" aria-labelledby="h-webhooks">
        <div class="section-head"><h2 id="h-webhooks">{t('health_page.unreadable_webhooks')}</h2></div>
        <ul class="entries">
          {#each view.unreadableWebhooks as entry, i (i)}
            <HealthEntry label={entry.subject ?? ''} code={entry.code} action={entry.action} />
          {/each}
        </ul>
      </section>
    {/if}

    <section class="section" aria-labelledby="h-types">
      <div class="section-head"><h2 id="h-types">{t('health_page.library_types')}</h2></div>
      {#if view.libraryTypeChanges.length === 0}
        <p class="muted">{t('health_page.library_types_none')}</p>
      {:else}
        <ul class="entries">
          {#each view.libraryTypeChanges as entry, i (i)}
            <HealthEntry label={entry.subject ?? ''} code={entry.code} action={entry.action} />
          {/each}
        </ul>
      {/if}
    </section>

    {#if view.relay.length > 0}
      <section class="section" aria-labelledby="h-relay">
        <div class="section-head"><h2 id="h-relay">{t('health_page.relay')}</h2></div>
        <ul class="entries">
          {#each view.relay as entry, i (i)}
            <HealthEntry
              label={t(
                entry.code.startsWith('kosync.')
                  ? 'health_page.relay_documents'
                  : entry.code.startsWith('page')
                    ? 'health_page.pages_set_aside'
                    : 'health_page.relay_chapters',
                { count: entry.count, subject: entry.subject ?? '' }
              )}
              code={entry.code}
              action={null}
            />
          {/each}
        </ul>
      </section>
    {/if}

    {#if view.withdrawalPending.count > 0 || (view.jurisdiction && !view.jurisdiction.confirmed)}
      <section class="section" aria-labelledby="h-waits">
        <div class="section-head"><h2 id="h-waits">{t('health_page.waits')}</h2></div>
        <ul class="entries">
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

    <section class="section" aria-labelledby="h-stats">
      <div class="section-head"><h2 id="h-stats">{t('health_page.stats')}</h2></div>
      <dl class="meta-list num">
        <dt>{t('health_page.stats_waiting')}</dt>
        <dd>{view.stats.waitingStates}</dd>
        <dt>{t('health_page.stats_exported')}</dt>
        <dd>{view.stats.unitsExported}</dd>
        <dt>{t('health_page.stats_arrivals')}</dt>
        <dd>{view.stats.arrivalsPending}</dd>
      </dl>
    </section>

    <section class="section" aria-labelledby="h-caches">
      <div class="section-head"><h2 id="h-caches">{t('health_page.caches')}</h2></div>
      <dl class="caches">
        {#each view.caches as cache (cache.kind)}
          <div class="cache">
            <dt>{t(`health_page.cache_kind_${cache.kind}`)}</dt>
            <dd>
              <span class="num">
                {#if cache.quotaBytes === null}
                  {t('health_page.cache_rows', { entries: cache.entries, quota: cache.quotaEntries ?? 0 })}
                {:else if cache.usedBytes === null}
                  {t('health_page.cache_entries', { entries: cache.entries, quota: cache.quotaBytes })}
                {:else}
                  {t('health_page.cache_bytes', {
                    entries: cache.entries,
                    used: cache.usedBytes,
                    quota: cache.quotaBytes
                  })}
                {/if}
              </span>
              <Badge tone={cache.over ? 'danger' : 'success'}>{t(cache.over ? 'health_page.cache_over' : 'health_page.cache_within')}</Badge>
              {#if cache.quotaBytes && cache.usedBytes !== null}
                <Progress
                  value={cache.usedBytes / cache.quotaBytes}
                  label={t(`health_page.cache_kind_${cache.kind}`)}
                  tone={cache.over ? 'danger' : 'accent'}
                />
              {/if}
            </dd>
          </div>
        {/each}
      </dl>
    </section>

    <section class="section" aria-labelledby="h-diagnostic">
      <div class="panel diagnostic">
        <div>
          <h2 id="h-diagnostic">{t('health_page.diagnostic')}</h2>
          <p class="muted">{t('health_page.diagnostic_help')}</p>
        </div>
        <Button href="/api/v1/diagnostic/bundle" download="diagnostic-bundle.zip" icon="download">
          {t('health_page.diagnostic_download')}
        </Button>
      </div>
    </section>
  {/if}
</main>

<style>
  .more {
    margin: var(--space-2) 0 0;
  }
  .sub {
    margin: var(--space-5) 0 var(--space-1);
    font-size: var(--step-1);
  }
  .caches {
    margin: 0;
    border-top: var(--rule-hair) solid var(--color-ink);
  }
  .cache {
    display: grid;
    grid-template-columns: minmax(10rem, 1fr) minmax(0, 2fr);
    gap: var(--space-1) var(--space-4);
    padding: var(--space-3) 0;
    border-bottom: var(--rule-hair) solid var(--color-rule);
  }
  .cache dt {
    font-weight: 600;
  }
  .cache dd {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2) var(--space-3);
  }
  .cache dd :global(.progress) {
    flex: 1 0 100%;
  }
  .diagnostic {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-4);
  }
  .diagnostic h2 {
    margin: 0 0 var(--space-1);
    font-size: var(--step-2);
  }
  .diagnostic p {
    margin: 0;
    max-width: var(--measure);
  }
  @media (max-width: 40rem) {
    .cache {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
