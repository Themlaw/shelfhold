<script lang="ts">
  // The reader (M5 L10 and L12, EF-LEC-001 to EF-LEC-003, EF-UX-006), mounted by the entry
  // `reader.html`, which is not a route of the SPA (ADR-0055). The screen knows the port
  // `ReaderEngine` and the registry, and nothing of how a page is drawn: an engine is loaded on
  // demand when a book is opened. Every byte comes through the links of the publication, that is
  // through the content route, which asks the profile's visibility again at each page. An extract
  // says so, writes nothing and ends on a sentence. Titles and every word of a book are inserted
  // as text, never as markup (the page itself allows inline styles).
  // The settings and the keyboard are L13's (EF-LEC-006, EF-LEC-011, ENF-A11Y-002): the settings
  // follow the priority the server resolves (a series beats the mode, a device beats the default),
  // a comfort change is asked its portée — « pour cet appareil » ou « partout » — and the tab
  // order is the ADR's: the bar, the content, then the pagination.
  import { onDestroy, onMount } from 'svelte';
  import { readerPublication, readerPutPosition, readerPutSettings, readerSettings } from '#lib/api/index.ts';
  import OfflineKeep from '#lib/offline/OfflineKeep.svelte';
  import { offlineStore } from '#lib/offline/runtime.ts';
  import { createQueueingPut } from '#lib/offline/sync.ts';
  import type { PositionIn, PublicationOut } from '#lib/api/types.gen.ts';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { readerDeviceId } from '#lib/reader/device.ts';
  import { detectBrowser, type Browser } from '#lib/reader/browser.ts';
  import { checkReaderCsp, type GuardVerdict } from '#lib/reader/csp-guard.ts';
  import {
    toView,
    type Locator,
    type PublicationView,
    type ReaderEngine,
    type ReadingDirection
  } from '#lib/reader/engine.ts';
  import { createPositionSender, type PositionSender } from '#lib/reader/positions.ts';
  import { EPUB_TYPE, EngineRefusedError, defaultRegistry } from '#lib/reader/registry.ts';
  import {
    effectiveDirection,
    effectiveFit,
    settingBody,
    toSettingsView,
    type Fit,
    type PendingSetting,
    type SettingsView,
    type SettingScope
  } from '#lib/reader/settings.ts';
  import { readerShortcut } from '#lib/reader/shortcuts.ts';
  import Badge from '#lib/ui/atoms/Badge.svelte';
  import Button from '#lib/ui/atoms/Button.svelte';
  import Icon from '#lib/ui/atoms/Icon.svelte';
  import IconButton from '#lib/ui/atoms/IconButton.svelte';
  import Progress from '#lib/ui/atoms/Progress.svelte';
  import Radio from '#lib/ui/atoms/Radio.svelte';
  import Skeleton from '#lib/ui/atoms/Skeleton.svelte';
  import Alert from '#lib/ui/molecules/Alert.svelte';
  import ErrorState, { type ErrorKind } from '#lib/ui/molecules/ErrorState.svelte';

  const DIRECTIONS: Record<ReadingDirection, 'reader.direction_ltr' | 'reader.direction_rtl' | 'reader.direction_webtoon'> = {
    ltr: 'reader.direction_ltr',
    rtl: 'reader.direction_rtl',
    webtoon: 'reader.direction_webtoon'
  };
  const FIT_LABELS: Record<Fit, 'reader.fit_contain' | 'reader.fit_width'> = {
    contain: 'reader.fit_contain',
    width: 'reader.fit_width'
  };

  interface Props {
    carrierType: string;
    carrierId: string;
    scope: 'excerpt' | null;
    /** The check of the policy in force; replaced by the tests, which have none. */
    guard?: () => Promise<GuardVerdict>;
    browser?: Browser | null;
  }
  let { carrierType, carrierId, scope, guard = () => checkReaderCsp(), browser }: Props = $props();

  let host = $state<HTMLElement | null>(null);
  let toolbar = $state<HTMLElement | null>(null);
  let problem = $state<ProblemView | null>(null);
  let problemStatus = $state<number | null>(null); // the HTTP status of the refusal, for the words around it
  let unsupported = $state<'format' | 'browser' | 'policy' | null>(null);
  let view = $state<PublicationView | null>(null);
  let at = $state<Locator | null>(null);
  let finished = $state(false);
  let failed = $state<'load' | 'display' | null>(null);
  let offline = $state(false); // read from a copy kept on this device
  let unavailable = $state(false); // no network and no copy
  let settings = $state<SettingsView | null>(null);
  let panel = $state(false); // the settings of the reader
  let help = $state(false); // the list of the shortcuts
  let pending = $state<PendingSetting | null>(null); // a change asked its portée
  let engine: ReaderEngine | null = null;
  let sender: PositionSender | null = null;
  let stops: (() => void)[] = [];
  let left = false;
  // Nothing is written before the reader moved: opening a book is not a reading position, and the
  // place another device holds must not be replaced by the first page just because the book opened.
  let moved = false;

  const asksExcerpt = $derived(scope === 'excerpt');
  const isExcerpt = $derived(view?.scope === 'excerpt');
  // « Lire hors ligne » (M6 L20, D-P7 (b)): every profile that opens a book may keep it — an
  // extract never offers it (D-P1: an extract is never kept)
  const keepable = $derived(view !== null && view.scope === 'full' && !asksExcerpt);
  // The EPUB engine exposes no way to apply a choice (L12): a book of pages reads with the
  // setting, an EPUB keeps the direction of its publication.
  const direction = $derived(
    view ? (view.kind === 'pages' ? effectiveDirection(view, settings) : view.readingDirection) : 'ltr'
  );
  const fit = $derived(effectiveFit(settings));
  const status = $derived(
    !at
      ? ''
      : at.type === 'page'
        ? t('reader.status', { n: at.index + 1, count: at.count })
        : t('reader.status_epub', { percent: Math.round(at.totalProgression * 100) })
  );

  // the first and the last page are said, never guessed from a disabled arrow alone
  const atFirst = $derived(at?.type === 'page' && at.count > 1 && at.index === 0);
  const atLast = $derived(at?.type === 'page' && at.count > 1 && at.index === at.count - 1);
  // how far the reading went, 0 to 1 (a book of pages by its page, an EPUB by its own measure)
  const progress = $derived(
    !at ? 0 : at.type === 'page' ? (at.count > 0 ? (at.index + 1) / at.count : 0) : at.totalProgression
  );
  const loading = $derived(!at && !failed);
  // the words of a refusal: the kind follows the status, a domain refusal (409) reads as « nothing here »
  const problemKind = $derived<ErrorKind>(
    problemStatus === 401
      ? 'signed-out'
      : problemStatus === 403
        ? 'forbidden'
        : problemStatus !== null && problemStatus >= 500
          ? 'server'
          : 'not-found'
  );

  const path = () => ({ carrier_type: carrierType, carrier_id: carrierId });

  /** The copy kept for this book by the profile signed in on this browser, if there is one. */
  async function keptCopy(): Promise<{ publication: PublicationOut; profile: string } | null> {
    try {
      const store = offlineStore();
      const profile = await store.activeProfile();
      const token = csrfToken();
      // another session than the one the copies were kept under (another profile signed in): none is opened
      if (token && (await store.sessionMatches(token)) === false) return null;
      const copy = profile ? await store.find(profile, carrierType, carrierId) : undefined;
      return profile && copy ? { publication: copy.publication, profile } : null;
    } catch {
      return null;
    }
  }

  async function activeProfileId(): Promise<string | null> {
    try {
      return await offlineStore().activeProfile();
    } catch {
      return null;
    }
  }

  /** The same book again, with new tickets and the same scope: a reading outlives a ticket. */
  async function renew(): Promise<PublicationView | null> {
    if (offline) return view; // a copy has no ticket to renew
    const { data } = await readerPublication({ path: path(), query: { scope: view?.scope ?? 'full' } });
    return data ? toView(data) : null;
  }

  /** What the profile chose for this series and this device; a book opens even without it. */
  async function readSettings(): Promise<void> {
    if (!view) return;
    const { data } = await readerSettings({
      query: { series_id: view.seriesId, device_id: readerDeviceId() }
    });
    if (!left && data) settings = toSettingsView(data);
  }

  /** Draws the book with an engine of the registry, the direction and the fit in hand; keeps the
   * place the reader is on. */
  async function mount(keep: Locator | null = null): Promise<void> {
    if (left || !host || !view) return;
    let found: ReaderEngine;
    try {
      const registry = defaultRegistry({ browser: browser === undefined ? detectBrowser(navigator) : browser, renew });
      found = await registry.engineFor(view.kind === 'epub' ? EPUB_TYPE : (view.pages[0]?.mediaType ?? ''));
    } catch (e) {
      unsupported = e instanceof EngineRefusedError && e.reason === 'browser' ? 'browser' : 'format';
      return;
    }
    if (left || !host) {
      found.destroy();
      return;
    }
    for (const stop of stops) stop();
    engine?.destroy();
    engine = found;
    stops = [
      engine.onLocator((locator) => {
        at = locator;
        finished = false;
        if (moved) sender?.push(locator);
      }),
      engine.onEnd(() => (finished = true)),
      engine.onError((e) => (failed = e.kind === 'undisplayable' ? 'display' : 'load'))
    ];
    await engine.open({ ...view, readingDirection: direction }, host);
    if (view.kind === 'pages') at = keep ?? { v: 1, type: 'page', index: 0, count: view.pages.length };
    if (keep) engine.goTo(keep);
  }

  async function open(): Promise<void> {
    // nothing is asked, nothing is drawn, under a policy that is not the reader's (ADR-0055)
    if ((await guard()) !== 'ok') {
      unsupported = 'policy';
      return;
    }
    if (left) return;
    let data: PublicationOut | undefined;
    let error: unknown;
    let profile: string | null = null;
    let httpStatus: number | null = null;
    try {
      const got = await readerPublication({
        path: path(),
        query: asksExcerpt ? { scope: 'excerpt' } : undefined
      });
      if (got.response === undefined) throw got.error; // the client returns a failed fetch instead of throwing it
      httpStatus = got.response.status;
      ({ data, error } = got);
    } catch {
      // no network: the copy this profile kept on this device (M5 L16b), never an extract's
      const copy = asksExcerpt ? null : await keptCopy();
      if (copy) {
        data = copy.publication;
        profile = copy.profile;
        offline = true;
      } else {
        unavailable = true;
        return;
      }
    }
    if (left) return;
    if (error !== undefined || !data) {
      problem = problemView(error);
      problemStatus = httpStatus;
      return;
    }
    view = toView(data);
    profile ??= await activeProfileId();
    const send = (body: PositionIn) =>
      readerPutPosition({
        body,
        headers: { 'X-CSRF-Token': csrfToken() },
        keepalive: true, // the last position still leaves when the tab is closing
        throwOnError: true
      });
    sender = createPositionSender({
      carrier: { type: data.carrier_type, id: data.carrier_id },
      scope: data.scope,
      deviceId: readerDeviceId(),
      // with a profile on this device, what cannot leave waits in its queue with the time it was read at
      put:
        profile
          ? createQueueingPut({
              store: offlineStore(),
              profileId: profile,
              sessionOk: async () => {
                const token = csrfToken();
                return !token || (await offlineStore().sessionMatches(token)) !== false;
              },
              put: async (body) => {
                const { response } = await readerPutPosition({ body, headers: { 'X-CSRF-Token': csrfToken() }, keepalive: true });
                if (response === undefined) throw new TypeError('network'); // a failed fetch is returned, not thrown
                const retry = Number(response.headers.get('Retry-After'));
                return { status: response.status, ...(Number.isFinite(retry) && retry > 0 ? { retryAfter: retry } : {}) };
              }
            })
          : send
    });
    await readSettings();
    await mount();
  }

  function turn(action: 'next' | 'previous'): void {
    moved = true;
    if (action === 'next') engine?.next();
    else engine?.previous();
  }

  function turnTo(index: number): void {
    if (!view || view.kind !== 'pages') return;
    moved = true;
    engine?.goTo({ v: 1, type: 'page', index, count: view.pages.length });
  }

  /** Writes one choice with its portée. The answer of a comfort write is resolved without the
   * series (a comfort's name says nothing of the book that is open), so only the field that was
   * written is taken from it: the direction the profile pinned to this series stays what the
   * reader read it to be. */
  async function write(change: PendingSetting, scope: SettingScope): Promise<void> {
    const { data } = await readerPutSettings({
      body: settingBody(change, scope, readerDeviceId()),
      headers: { 'X-CSRF-Token': csrfToken() },
      throwOnError: false
    });
    if (left || !data) return;
    settings = {
      direction: change.kind === 'direction' ? data.direction : (settings?.direction ?? null),
      comfort: change.kind === 'direction' ? (settings?.comfort ?? {}) : data.comfort
    };
  }

  /** The sens of this series: written for the series, and the book is drawn again at once. */
  async function changeDirection(value: ReadingDirection): Promise<void> {
    if (!view || view.kind !== 'pages' || direction === value) return;
    const before = at;
    settings = { direction: value, comfort: settings?.comfort ?? {} };
    await mount(before);
    await write({ kind: 'direction', key: view.seriesId, value }, 'everywhere');
  }

  /** A comfort change is asked its portée before it is written (EF-LEC-006). */
  function askFit(value: Fit): void {
    if (fit === value) return;
    pending = { kind: 'comfort', key: 'fit', value };
  }

  async function answer(scope: SettingScope): Promise<void> {
    const change = pending;
    pending = null;
    if (!change) return;
    settings = {
      direction: settings?.direction ?? null,
      comfort: { ...settings?.comfort, fit: change.value }
    };
    await write(change, scope);
  }

  function onKey(event: KeyboardEvent): void {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'Escape') {
      // the focus is never trapped: Escape gives it back to the controls, from anywhere
      help = false;
      pending = null;
      panel = false;
      toolbar?.querySelector<HTMLElement>('button, a')?.focus();
      return;
    }
    // a key relayed from a document of the book has the window for target, which is not an element
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest('input, textarea, select')) return; // a control keeps its own keys
    if (!view) return;
    const action = readerShortcut(direction, event.key);
    if (action === null) return;
    if (action === 'help') {
      event.preventDefault();
      help = !help;
      return;
    }
    if (target?.closest('button, a') && (event.key === ' ' || event.key === 'Home' || event.key === 'End')) {
      return; // a focused control keeps its key
    }
    if (direction === 'webtoon' && (action === 'next' || action === 'previous')) {
      return; // a column scrolls by itself
    }
    event.preventDefault();
    if (action === 'first') turnTo(0);
    else if (action === 'last') turnTo((view.pages.length || 1) - 1);
    else turn(action);
  }

  function leave(): void {
    void sender?.flush();
  }

  // a column moves by scrolling: the first scroll of the reader is the first move
  const scrolled = () => (moved = true);

  onMount(() => {
    void open();
    window.addEventListener('pagehide', leave);
    window.addEventListener('scroll', scrolled, { passive: true, once: true });
  });

  onDestroy(() => {
    left = true;
    if (typeof window !== 'undefined') {
      window.removeEventListener('pagehide', leave);
      window.removeEventListener('scroll', scrolled);
    }
    leave();
    for (const stop of stops) stop();
    engine?.destroy();
  });
</script>

<svelte:window onkeydown={onKey} />

<svelte:head>
  <title>{view?.title ?? t('reader.loading')}</title>
</svelte:head>

<main class="reader" data-direction={direction}>
  {#if unavailable}
    <div class="state" role="alert">
      <ErrorState kind="offline" code={t('error.offline.code')} title={t('error.offline.title')}>
        <p>{t('reader.offline_missing')}</p>
        {#snippet actions()}
          <div class="btn-row">
            <Button href="/hors-ligne" variant="primary" icon="offline">{t('offline.library')}</Button>
            <Button icon="refresh" onclick={() => location.reload()}>{t('error.retry')}</Button>
          </div>
        {/snippet}
      </ErrorState>
    </div>
  {:else if problem}
    <div class="state" role="alert">
      <ErrorState
        kind={problemKind}
        code={problemStatus ? t('error.code', { status: problemStatus }) : undefined}
        title={problem.title}
        incident={problem.requestId}
      >
        {#if problem.cause}<p>{problem.cause}</p>{/if}
        {#if problem.action}<p>{problem.action}</p>{/if}
        {#snippet actions()}
          <div class="btn-row">
            {#if problemKind === 'signed-out'}
              <Button href="/login" variant="primary" icon="key">{t('ui.account.sign_in')}</Button>
            {:else if problemKind === 'server'}
              <Button variant="primary" icon="refresh" onclick={() => location.reload()}>{t('error.retry')}</Button>
              <Button href="/" icon="home">{t('error.back_home')}</Button>
            {:else}
              <Button href="/" variant="primary" icon="home">{t('error.back_home')}</Button>
            {/if}
          </div>
        {/snippet}
      </ErrorState>
    </div>
  {:else if unsupported}
    <div class="state" role="alert">
      <ErrorState kind={unsupported === 'policy' ? 'server' : 'not-found'} title={t('reader.unreadable_title')}>
        <p>
          {unsupported === 'policy'
            ? t('reader.csp_refused')
            : unsupported === 'browser'
              ? t('reader.browser_unsupported')
              : t('reader.unsupported')}
        </p>
        {#snippet actions()}
          <div class="btn-row">
            {#if unsupported === 'policy'}
              <Button variant="primary" icon="refresh" onclick={() => location.reload()}>{t('error.retry')}</Button>
            {/if}
            <Button href="/" variant={unsupported === 'policy' ? 'secondary' : 'primary'} icon="home">{t('error.back_home')}</Button>
          </div>
        {/snippet}
      </ErrorState>
    </div>
  {:else}
    <header class="bar" bind:this={toolbar} role="group" aria-label={t('reader.toolbar')}>
      <IconButton icon="left" label={t('reader.close')} onclick={() => history.back()} />
      <div class="bar__title">
        <h1>{view?.title ?? t('reader.loading')}</h1>
      </div>
      {#if isExcerpt}<Badge tone="accent" icon="eye">{t('reader.excerpt_badge')}</Badge>{/if}
      {#if offline}<Badge icon="offline">{t('reader.offline_badge')}</Badge>{/if}
      <div class="bar__tools">
        {#if view?.kind === 'pages'}
          <IconButton
            icon="type"
            label={t('reader.settings')}
            variant={panel ? 'secondary' : 'ghost'}
            pressed={panel}
            aria-expanded={panel}
            onclick={() => (panel = !panel)}
          />
        {/if}
        {#if keepable}<OfflineKeep {carrierType} {carrierId} compact />{/if}
        <IconButton icon="help" label={t('reader.help')} pressed={help} aria-expanded={help} onclick={() => (help = !help)} />
      </div>
    </header>
    <!-- said once to a screen reader, shown nowhere: the keys are in the help -->
    <p class="visually-hidden" id="reader-keys">{t('reader.keys')}</p>
    {#if failed}
      <div class="notice">
        <Alert tone="danger" live="assertive" title={failed === 'display' ? t('reader.page_undisplayable') : t('reader.page_failed')} />
      </div>
    {/if}
    <div class="stage" data-surface="paper">
      {#if loading}
        <div class="loading" aria-hidden="true"><Skeleton shape="block" /></div>
      {/if}
      <div
        class="pages"
        bind:this={host}
        role="region"
        aria-label={t('reader.pages')}
        aria-describedby="reader-keys"
        aria-busy={loading}
        data-fit={fit}
      ></div>
    </div>
    <!-- the tab order of the ADR: the bar, the content, then the pagination -->
    <footer class="foot">
      {#if finished}
        <p class="end" data-reader-end>
          <Icon name="check" />
          {isExcerpt ? t('reader.end_of_excerpt') : t('reader.end_of_book')}
        </p>
      {/if}
      <div class="foot__row">
        <nav class="turning" aria-label={t('reader.turning')}>
          <IconButton
            icon="left"
            variant="secondary"
            label={t('reader.previous')}
            onclick={() => turn('previous')}
            aria-disabled={!at || (at.type === 'page' && at.index === 0)}
          />
          <IconButton icon="right" variant="secondary" label={t('reader.next')} onclick={() => turn('next')} aria-disabled={!at} />
        </nav>
        <div class="foot__progress">
          <p role="status" class="folio foot__status">{status}</p>
          {#if atFirst && !finished}<span class="foot__edge">{t('reader.first_page')}</span>{/if}
          {#if atLast && !finished}<span class="foot__edge">{t('reader.last_page')}</span>{/if}
          <div class="foot__rule"><Progress value={progress} label={t('reader.progress')} /></div>
        </div>
      </div>
    </footer>
    {#if panel && view?.kind === 'pages'}
      <section class="sheet" aria-label={t('reader.settings')} data-reader-settings>
        <span class="sheet__grip" aria-hidden="true"></span>
        <div class="sheet__head">
          <h2>{t('reader.settings')}</h2>
          <IconButton icon="x" label={t('reader.close_settings')} onclick={() => (panel = false)} />
        </div>
        <fieldset>
          <legend>{t('reader.settings_direction')}</legend>
          {#each Object.entries(DIRECTIONS) as [value, label] (value)}
            <Radio
              name="reader-direction"
              {value}
              group={direction}
              label={t(label)}
              onchange={(event) => {
                // a browser may tell the radio that lost the selection too: only the winner writes
                if (event.currentTarget.checked) void changeDirection(value as ReadingDirection);
              }}
            />
          {/each}
        </fieldset>
        <fieldset>
          <legend>{t('reader.fit')}</legend>
          {#each Object.entries(FIT_LABELS) as [value, label] (value)}
            <Radio
              name="reader-fit"
              {value}
              group={fit}
              label={t(label)}
              onchange={(event) => {
                if (event.currentTarget.checked) askFit(value as Fit);
              }}
            />
          {/each}
        </fieldset>
      </section>
    {/if}
    {#if pending}
      <div class="dock" role="group" aria-label={t('reader.scope_question')} data-reader-scope>
        <!-- a question that appears must be told: the region is polite, the reader hears it -->
        <Alert tone="info" live="polite" title={t('reader.scope_question')}>
          {#snippet actions()}
            <Button variant="primary" size="sm" onclick={() => void answer('device')}>{t('reader.scope_device')}</Button>
            <Button size="sm" onclick={() => void answer('everywhere')}>{t('reader.scope_everywhere')}</Button>
            <Button variant="ghost" size="sm" onclick={() => (pending = null)}>{t('reader.cancel')}</Button>
          {/snippet}
        </Alert>
      </div>
    {/if}
    {#if help}
      <section class="sheet" aria-label={t('reader.help')} data-reader-help>
        <span class="sheet__grip" aria-hidden="true"></span>
        <div class="sheet__head">
          <h2>{t('reader.help')}</h2>
          <IconButton icon="x" label={t('reader.close_help')} onclick={() => (help = false)} />
        </div>
        <ul class="keys">
          <li>{t('reader.help_turn')}</li>
          <li>{t('reader.help_bounds')}</li>
          <li>{t('reader.help_help')}</li>
          <li>{t('reader.help_escape')}</li>
        </ul>
      </section>
    {/if}
  {/if}
</main>

<style>
  .reader {
    display: flex;
    flex-direction: column;
    min-height: 100dvh;
    background: var(--color-bg);
    color: var(--color-ink);
  }
  .state {
    padding: var(--space-5) var(--space-4);
  }

  /* the bars: quiet, one line, the book comes first */
  .bar,
  .foot {
    position: sticky;
    z-index: var(--z-sticky);
    background: var(--color-surface-chrome);
    border-color: var(--color-rule);
  }
  .bar {
    top: 0;
    display: flex;
    align-items: center;
    gap: var(--space-2) var(--space-3);
    padding: var(--space-2) var(--space-3);
    border-bottom: var(--rule-hair) solid var(--color-rule);
  }
  .bar__title {
    display: grid;
    flex: 1 1 8rem;
    min-width: 0;
  }
  .bar h1 {
    margin: 0;
    font: 600 var(--step-05) / var(--leading-tight) var(--font-text);
    overflow-wrap: anywhere;
  }
  .bar__tools {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: var(--space-1);
    margin-left: auto;
  }
  .notice {
    padding: var(--space-3) var(--space-4) 0;
  }

  /* the stage: the paper reading ground. The board of a page of images is paper whatever the
     scheme: a page drawn on white is never turned to black by the dark theme */
  .stage {
    position: relative;
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    padding: var(--space-4) var(--space-3);
    background: var(--color-bg);
  }
  .loading {
    position: absolute;
    inset: var(--space-4) var(--space-3);
    display: grid;
    place-items: center;
    pointer-events: none;
  }
  .loading :global(.skeleton) {
    width: min(100%, 26rem);
    aspect-ratio: 2 / 3;
  }
  .pages {
    display: flex;
    justify-content: center;
    min-height: 50dvh;
  }
  .pages :global([data-reader-page]) {
    display: flex;
    justify-content: center;
    max-height: calc(100dvh - 11rem);
    background: var(--cv-paper);
    box-shadow: var(--shadow-2);
  }
  .pages :global([data-reader-page] img) {
    max-height: calc(100dvh - 11rem);
    object-fit: contain;
  }
  /* the comfort setting of the reader: a page may fill the width of the screen */
  .pages[data-fit='width'] :global([data-reader-page]) {
    max-height: none;
  }
  .pages[data-fit='width'] :global([data-reader-page] img) {
    width: 100%;
    height: auto;
  }
  .pages :global([data-reader-column]) {
    width: 100%;
    background: var(--cv-paper);
  }

  /* the foot: the pagination, the folio and the progress */
  .foot {
    bottom: 0;
    padding: var(--space-2) var(--space-3);
    border-top: var(--rule-hair) solid var(--color-rule);
  }
  .foot__row {
    display: flex;
    align-items: center;
    gap: var(--space-3);
  }
  .turning {
    display: flex;
    gap: var(--space-2);
  }
  .turning :global(.icon-btn[aria-disabled='true']) {
    opacity: 0.45;
  }
  .foot__progress {
    display: flex;
    flex: 1 1 auto;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-1) var(--space-3);
    min-width: 0;
  }
  .foot__status {
    margin: 0;
    font-size: var(--step-0);
  }
  .foot__edge {
    color: var(--color-muted);
    font-size: var(--step--1);
  }
  .foot__rule {
    flex: 1 1 100%;
  }
  .end {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    margin: 0 0 var(--space-2);
    font: italic 600 var(--step-1) / var(--leading-ui) var(--font-text);
  }

  /* the sheets (settings, help): the bottom of a phone, the right edge of a large screen. They
     hold no focus: Escape or the close button, and the page behind stays readable */
  .sheet {
    position: fixed;
    z-index: var(--z-overlay);
    inset: auto 0 0 0;
    display: grid;
    gap: var(--space-4);
    max-height: 75dvh;
    overflow-y: auto;
    padding: var(--space-2) var(--space-4) var(--space-5);
    background: var(--color-surface-float);
    border-top: var(--rule-hair) solid var(--color-rule-strong);
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
    box-shadow: var(--shadow-3);
  }
  .sheet__grip {
    justify-self: center;
    inline-size: 2.5rem;
    block-size: var(--space-1);
    border-radius: var(--radius-pill);
    background: var(--color-rule-strong);
  }
  .sheet__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
  }
  .sheet h2 {
    margin: 0;
    font-size: var(--step-2);
  }
  .sheet fieldset {
    margin: 0;
    padding: 0;
    border: 0;
  }
  .sheet legend {
    padding: 0;
    margin-bottom: var(--space-1);
    font-weight: 700;
  }
  .keys {
    display: grid;
    gap: var(--space-2);
    margin: 0;
    padding-left: var(--space-5);
  }
  .dock {
    position: fixed;
    z-index: var(--z-dialog);
    inset: auto var(--space-3) var(--space-3) var(--space-3);
    max-width: 30rem;
    margin-inline: auto;
    box-shadow: var(--shadow-3);
  }
  @media (min-width: 48rem) {
    .reader {
      --panel-w: 24rem;
    }
    .sheet {
      inset: 0 0 0 auto;
      inline-size: var(--panel-w);
      max-height: none;
      padding-top: var(--space-5);
      border-top: 0;
      border-left: var(--rule-hair) solid var(--color-rule-strong);
      border-radius: 0;
    }
    .sheet__grip {
      display: none;
    }
    .dock {
      inset: auto var(--space-5) var(--space-5) auto;
      margin-inline: 0;
    }
  }
  /* a large screen: the bars take their words back */
  @media (min-width: 48rem) {
    .bar,
    .foot {
      padding-inline: var(--space-5);
    }
    .stage {
      padding-block: var(--space-5);
    }
  }
</style>
