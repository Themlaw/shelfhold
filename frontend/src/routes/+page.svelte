<script lang="ts">
  // The home page (EF-UX-005, EF-LEC-008, EF-UX-010): where the reader stopped (« Vous en étiez
  // là », the first entry of « Reprendre la lecture »), the other books under way, what waits for
  // the administrator's approval, what arrived lately, and the reader's own requests. The lists
  // are the server's, filtered for the reader on the server (a child is shown no Work that is
  // hidden from her): the screen shows what it is given, and an empty answer is one state
  // whatever the reason. A list the server could not give is said in its own block; the rest of
  // the page stands.
  import { onMount } from 'svelte';
  import { listWorks, mine as myRequests, queue as approvalQueue, readerContinue } from '#lib/api/index.ts';
  import type { ContinueItemOut, QueueItemOut, RequestOut, WorkItemOut } from '#lib/api/types.gen.ts';
  import HealthStatus from '#lib/components/HealthStatus.svelte';
  import RequestItem from '#lib/components/RequestItem.svelte';
  import { formatDate, formatNumber, hasMessage, t } from '#lib/i18n/index.ts';
  import { webAddress } from '#lib/links.ts';
  import { reloadOnStream } from '#lib/live.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Cover from '#lib/ui/atoms/Cover.svelte';
  import { coverKind } from '#lib/ui/atoms/cover.ts';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import EmptyState from '#lib/ui/molecules/EmptyState.svelte';
  import SeriesCard from '#lib/ui/molecules/SeriesCard.svelte';
  import Shelf from '#lib/ui/organisms/Shelf.svelte';

  type User = { name: string; role: 'admin' | 'member' | 'child' } | null;
  let { data }: { data?: { user?: User } } = $props();

  const RECENT = 12;
  const RESUME = 6;
  const APPROVALS = 3;

  const user = $derived(data?.user ?? null);
  const admin = $derived(user?.role === 'admin');
  const child = $derived(user?.role === 'child');

  let works = $state<WorkItemOut[]>([]);
  let requests = $state<RequestOut[]>([]);
  let resume = $state<ContinueItemOut[]>([]);
  let pending = $state<QueueItemOut[]>([]);
  let failed = $state({ works: false, requests: false });
  let loaded = $state(false);

  /** A refusal of the session (401, 403) is the layout's business: here it is an empty list. */
  const broken = (response: Response | undefined, error: unknown) =>
    error !== undefined && (response === undefined || response.status >= 500);

  async function load(): Promise<void> {
    const [recent, own, again, waiting] = await Promise.all([
      listWorks({ query: { sort: 'recent', limit: RECENT } }).catch(() => ({ data: undefined, error: true, response: undefined })),
      myRequests().catch(() => ({ data: undefined, error: true, response: undefined })),
      readerContinue({ query: { limit: RESUME } }).catch(() => ({ data: undefined })),
      admin ? approvalQueue().catch(() => ({ data: undefined })) : Promise.resolve({ data: undefined })
    ]);
    resume = again.data?.items ?? [];
    works = recent.data?.items ?? [];
    requests = own.data?.items ?? [];
    pending = waiting.data?.items ?? [];
    failed = { works: broken(recent.response, recent.error), requests: broken(own.response, own.error) };
    loaded = true;
  }

  onMount(() => {
    void load();
    return reloadOnStream(load);
  });

  // The kind of the destination, as the interface names it (the slug itself when it is unknown).
  const destinationName = (kind: string): string => {
    const key = `destination.name.${kind}`;
    return hasMessage(key) ? t(key) : kind;
  };

  /** Where an entry of « Reprendre la lecture » opens: the copy kept here, or its own server. */
  function opening(item: ContinueItemOut) {
    const verb =
      item.target === 'kept_copy'
        ? t('home.continue_read')
        : t('home.continue_open_in', { destination: destinationName(item.target) });
    const label = t('home.continue_open', { open: verb, title: item.title, number: item.number });
    if (item.target === 'kept_copy' && item.carrier_type && item.carrier_id) {
      const href = `/lire/${encodeURIComponent(item.carrier_type)}/${encodeURIComponent(item.carrier_id)}`;
      return { href, external: false, verb: t('home.continue_read'), label };
    }
    const href = webAddress(item.open_url);
    return href ? { href, external: true, verb, label } : null;
  }

  const where = (item: ContinueItemOut) =>
    [
      t('home.continue_unit', { number: item.number }),
      item.state === 'in_progress' ? t('home.continue_in_progress') : t('home.continue_next'),
      ...(item.approximate ? [t('home.continue_approximate')] : [])
    ].join(' · ');

  const hero = $derived(resume[0] ?? null);
  // the continue entry names no author nor kind: the list of works does, when it holds the work
  const heroWork = $derived(hero ? (works.find((work) => work.work_id === hero.work_id) ?? null) : null);
  const also = $derived(resume.slice(1));

  // one greeting whatever the hour: the screen reads the same at any time (its reference too)
  const greeting = $derived(user ? t('home.greeting', { name: user.name }) : t('home.greeting_anonymous'));
  const today = formatDate(new Date(), { weekday: 'long', day: 'numeric', month: 'long' });
  const thumb = (hash: string | null | undefined) => (hash ? `/api/v1/thumbnails/${hash}/320` : null);
</script>

<svelte:head>
  <title>{t('home.title')}</title>
</svelte:head>

<main class="page home">
  <div class="running-head">
    <span>{child && user ? t('ui.header.house_child', { name: user.name }) : t('ui.header.house')}</span>
    <span class="running-head__folio">{today}</span>
  </div>
  <div class="page-head">
    <div class="page-head__text">
      <h1 class="page-title">{greeting}</h1>
      <p class="lede">
        {#if admin && pending.length > 0}
          {t('home.lede_pending', { count: pending.length })}
        {:else}
          {child ? t('home.tagline_child') : t('home.tagline')}
        {/if}
      </p>
    </div>
    <div class="page-head__actions">
      <Button href="/chercher" icon="search">{t('home.search_book')}</Button>
    </div>
  </div>

  {#if !loaded}
    <div class="home__loading" aria-busy="true">
      <p class="visually-hidden" role="status">{t('home.loading')}</p>
      <div class="hero hero--loading">
        <div class="hero__object"><Skeleton shape="cover" /></div>
        <div class="hero__text"><Skeleton /><Skeleton /><Skeleton short /></div>
      </div>
      <Shelf label={t('home.loading')} busy>
        {#each { length: 6 }, index (index)}
          <div role="listitem"><Skeleton shape="cover" /></div>
        {/each}
      </Shelf>
    </div>
  {:else}
    {#if hero}
      {@const open = opening(hero)}
      <section class="hero" aria-labelledby="continue-title">
        <div class="hero__object">
          <div class="hero__stand">
            <Cover title={hero.title} seed={hero.work_id} author={heroWork?.author ?? null} kind={coverKind(heroWork?.kind)} size="fill" />
          </div>
        </div>
        <div class="hero__text">
          <h2 class="hero__kicker" id="continue-title">{child ? t('home.hero_child') : t('home.hero')}</h2>
          <p class="hero__title">{hero.title}</p>
          {#if heroWork?.author}<p class="hero__by">{heroWork.author}</p>{/if}
          {#if hero.unit_title}<p class="hero__unit">{hero.unit_title}</p>{/if}
          <p class="hero__where">{where(hero)}</p>
          {#if open}
            <div class="btn-row">
              <Button
                href={open.href}
                variant="primary"
                size="lg"
                icon={open.external ? 'external' : 'read'}
                aria-label={open.external ? open.label : t('home.continue_open', { open: t('home.resume'), title: hero.title, number: hero.number })}
                rel={open.external ? 'noopener noreferrer' : undefined}
              >
                {open.external ? open.verb : t('home.resume')}
              </Button>
            </div>
          {/if}
        </div>
      </section>
    {/if}

    {#if also.length > 0}
      <section class="section" aria-labelledby="also-title">
        <div class="section-head">
          <h2 id="also-title">{t('home.also_reading')}</h2>
          <span class="folio">{t('home.also_count', { count: also.length })}</span>
        </div>
        <ul class="resumes" role="list">
          {#each also as item (item.work_id)}
            {@const open = opening(item)}
            <li class="resume">
              <Cover title={item.title} seed={item.work_id} size="sm" />
              <div class="resume__text">
                <p class="resume__title">{item.title}</p>
                <p class="resume__where">{where(item)}</p>
                {#if open}
                  <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- an address of this app, or of the reader's server -->
                  <a class="resume__open standalone" href={open.href} aria-label={open.label} rel={open.external ? 'noopener noreferrer' : undefined}>{open.verb}</a>
                {/if}
              </div>
            </li>
          {/each}
        </ul>
      </section>
    {/if}

    {#if admin && pending.length > 0}
      <section class="section" aria-labelledby="approve-title">
        <div class="section-head">
          <h2 id="approve-title">{t('nav.approve')}</h2>
          <Badge tone="count" label={t('home.approve_count', { count: pending.length })}>{formatNumber(pending.length)}</Badge>
          <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
          <a class="section-head__more" href="/a-approuver">{t('home.approve_all')}</a>
        </div>
        <ul class="entries" role="list">
          {#each pending.slice(0, APPROVALS) as item (item.id)}
            <li class="entry">
              <Cover title={item.title ?? t('approve.masked')} size="sm" />
              <div class="entry__text">
                <p class="entry__title">{item.title ?? t('approve.masked')}</p>
                <p class="entry__meta hint">
                  {t(item.requester_role === 'child' ? 'approve.role.child' : item.requester_role === 'admin' ? 'approve.role.admin' : 'approve.role.member')}
                  · {formatDate(item.created_at, { day: 'numeric', month: 'long' })}
                </p>
              </div>
              <Button href="/a-approuver" size="sm">{t('home.approve_review')}</Button>
            </li>
          {/each}
        </ul>
      </section>
    {/if}

    <section class="section recently-arrived" aria-labelledby="recent-title">
      <div class="section-head">
        <h2 id="recent-title">{t('home.recently_arrived')}</h2>
        {#if works.length > 0}
          <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
          <a class="section-head__more" href="/bibliotheque">{t('home.whole_library')}</a>
        {/if}
      </div>
      {#if failed.works}
        <Alert tone="danger" title={t('home.block_failed')}>
          {#snippet actions()}<Button size="sm" icon="refresh" onclick={() => void load()}>{t('error.retry')}</Button>{/snippet}
        </Alert>
      {:else if works.length === 0}
        <EmptyState title={t('home.empty_library')} headingLevel={3} art={false}>
          {#snippet actions()}<Button href="/chercher" variant="primary">{t('home.add_first_book')}</Button>{/snippet}
        </EmptyState>
      {:else}
        <Shelf label={t('home.recently_arrived')} child={child}>
          {#each works as work (work.work_id)}
            <div role="listitem">
              <SeriesCard
                title={work.title}
                seed={work.work_id}
                author={work.author ?? null}
                src={thumb(work.cover_hash)}
                kind={coverKind(work.kind)}
                href={admin && work.edition_id ? `/serie/${work.edition_id}` : null}
              />
            </div>
          {/each}
        </Shelf>
      {/if}
    </section>

    <div class="cols section">
      <section class="my-requests" aria-labelledby="requests-title">
        <div class="section-head">
          <h2 id="requests-title">{t('home.my_requests')}</h2>
          {#if requests.length > 0}<span class="folio">{formatNumber(requests.length)}</span>{/if}
        </div>
        {#if failed.requests}
          <Alert tone="danger" title={t('home.block_failed')} />
        {:else if requests.length === 0}
          <EmptyState title={t('home.requests_empty')} headingLevel={3} art={false}>
            {#snippet actions()}<Button href="/chercher">{t('home.search_book')}</Button>{/snippet}
          </EmptyState>
        {:else}
          <ul class="entries" role="list">
            {#each requests as request (request.id)}
              <li><RequestItem {request} /></li>
            {/each}
          </ul>
        {/if}
      </section>
      {#if admin || !user}
        <!-- the state of the instance: the administrator's panel, and what a visitor sees before signing in -->
        <aside class="panel" aria-label={t('home.instance')}>
          <HealthStatus />
          <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin -->
          <p><a class="standalone" href="/sante">{t('home.open_health')}</a></p>
        </aside>
      {/if}
    </div>
  {/if}
</main>

<style>
  .section-head__more {
    display: inline-flex;
    align-items: center;
    min-height: var(--target);
  }
  .hero {
    display: grid;
    grid-template-columns: minmax(9rem, 13rem) minmax(0, 1fr);
    gap: var(--space-6);
    align-items: end;
    margin-top: var(--space-6);
    padding: var(--space-6);
    background: var(--color-surface);
    border: var(--rule-hair) solid var(--color-rule);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-1);
  }
  .hero__stand {
    padding: 0 var(--space-3);
    border-bottom: var(--rule-thick) solid var(--color-shelf);
  }
  .hero__text {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--space-2);
    min-width: 0;
  }
  .hero__kicker {
    margin: 0;
    font: italic 400 var(--step-1) / 1.2 var(--font-text);
    color: var(--color-muted);
  }
  .hero__title {
    margin: 0;
    font: 600 var(--step-4) / var(--leading-tight) var(--font-display);
    letter-spacing: -0.02em;
    text-wrap: balance;
    overflow-wrap: anywhere;
  }
  .hero__by {
    margin: 0;
    font: italic var(--step-1) / 1.3 var(--font-text);
  }
  .hero__unit {
    margin: 0;
    font: var(--step-0) / 1.3 var(--font-text);
    color: var(--color-muted);
  }
  .hero__where {
    margin: 0 0 var(--space-3);
    color: var(--color-muted);
  }
  .home__loading .hero {
    margin-bottom: var(--space-7);
  }
  .resumes {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(18rem, 1fr));
    gap: var(--space-5);
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .resume {
    display: grid;
    grid-template-columns: 4.5rem minmax(0, 1fr);
    gap: var(--space-4);
    align-items: start;
  }
  .resume__text {
    display: grid;
    gap: var(--space-1);
    min-width: 0;
  }
  .resume__title {
    margin: 0;
    font: 600 var(--step-05) / 1.25 var(--font-text);
    overflow-wrap: anywhere;
  }
  .resume__where {
    margin: 0;
    font-size: var(--step--1);
    color: var(--color-muted);
  }
  .entry {
    display: grid;
    grid-template-columns: 3rem minmax(0, 1fr) auto;
    gap: var(--space-4);
    align-items: center;
    padding: var(--space-3) 0;
  }
  .my-requests :global(.entries > li) {
    padding: var(--space-3) 0;
  }
  .entry__text {
    min-width: 0;
  }
  .entry__title {
    margin: 0;
    font-weight: 700;
    overflow-wrap: anywhere;
  }
  .entry__meta {
    margin: var(--space-1) 0 0;
  }
  @media (max-width: 40rem) {
    /* one column on a phone: the call to resume keeps its whole width */
    .hero {
      grid-template-columns: minmax(0, 1fr);
      gap: var(--space-4);
      padding: var(--space-4);
      align-items: start;
    }
    .hero__object {
      inline-size: 7.5rem;
    }
    .hero__title {
      font-size: var(--step-2);
    }
  }
</style>
