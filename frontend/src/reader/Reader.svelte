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
  import ProblemCard from '#lib/components/ProblemCard.svelte';
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
  let unsupported = $state<'format' | 'browser' | 'policy' | null>(null);
  let view = $state<PublicationView | null>(null);
  let at = $state<Locator | null>(null);
  let finished = $state(false);
  let failed = $state<'load' | 'display' | null>(null);
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

  const path = () => ({ carrier_type: carrierType, carrier_id: carrierId });

  /** The same book again, with new tickets and the same scope: a reading outlives a ticket. */
  async function renew(): Promise<PublicationView | null> {
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
    const { data, error } = await readerPublication({
      path: path(),
      query: asksExcerpt ? { scope: 'excerpt' } : undefined
    });
    if (left) return;
    if (error !== undefined || !data) {
      problem = problemView(error);
      return;
    }
    view = toView(data);
    sender = createPositionSender({
      carrier: { type: data.carrier_type, id: data.carrier_id },
      scope: data.scope,
      deviceId: readerDeviceId(),
      put: (body) =>
        readerPutPosition({
          body,
          headers: { 'X-CSRF-Token': csrfToken() },
          keepalive: true, // the last position still leaves when the tab is closing
          throwOnError: true
        })
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
  {#if problem}
    <ProblemCard {problem} level={1} />
  {:else if unsupported}
    <p role="alert">
      {unsupported === 'policy'
        ? t('reader.csp_refused')
        : unsupported === 'browser'
          ? t('reader.browser_unsupported')
          : t('reader.unsupported')}
    </p>
  {:else}
    <header bind:this={toolbar} role="group" aria-label={t('reader.toolbar')}>
      <button type="button" onclick={() => history.back()}>{t('reader.close')}</button>
      <h1>{view?.title ?? t('reader.loading')}</h1>
      {#if isExcerpt}<span class="badge">{t('reader.excerpt_badge')}</span>{/if}
      {#if view?.kind === 'pages'}
        <button
          type="button"
          aria-expanded={panel}
          onclick={() => (panel = !panel)}>{t('reader.settings')}</button
        >
      {/if}
      <button type="button" aria-expanded={help} onclick={() => (help = !help)}>?</button>
    </header>
    <p class="hint">{t('reader.keys')}</p>
    <p role="status" class="status">{status}</p>
    {#if failed}
      <p class="notice" role="alert">
        {failed === 'display' ? t('reader.page_undisplayable') : t('reader.page_failed')}
      </p>
    {/if}
    <div
      class="pages"
      bind:this={host}
      role="region"
      aria-label={t('reader.pages')}
      data-fit={fit}
    ></div>
    <!-- the tab order of the ADR: the bar, the content, then the pagination -->
    <nav class="turning" aria-label={t('reader.turning')}>
      <button
        type="button"
        onclick={() => turn('previous')}
        aria-disabled={!at || (at.type === 'page' && at.index === 0)}>{t('reader.previous')}</button
      >
      <button type="button" onclick={() => turn('next')} aria-disabled={!at}>{t('reader.next')}</button>
    </nav>
    {#if finished}
      <p class="end" data-reader-end>
        {isExcerpt ? t('reader.end_of_excerpt') : t('reader.end_of_book')}
      </p>
    {/if}
    {#if panel && view?.kind === 'pages'}
      <section class="panel" aria-label={t('reader.settings')} data-reader-settings>
        <fieldset>
          <legend>{t('reader.settings_direction')}</legend>
          {#each Object.entries(DIRECTIONS) as [value, label] (value)}
            <label>
              <input
                type="radio"
                name="reader-direction"
                value={value}
                checked={direction === value}
                onchange={(event) => {
                  // a browser may tell the radio that lost the selection too: only the winner writes
                  if ((event.currentTarget as HTMLInputElement).checked) {
                    void changeDirection(value as ReadingDirection);
                  }
                }}
              />
              {t(label)}
            </label>
          {/each}
        </fieldset>
        <fieldset>
          <legend>{t('reader.fit')}</legend>
          {#each Object.entries(FIT_LABELS) as [value, label] (value)}
            <label>
              <input
                type="radio"
                name="reader-fit"
                value={value}
                checked={fit === value}
                onchange={(event) => {
                  if ((event.currentTarget as HTMLInputElement).checked) askFit(value as Fit);
                }}
              />
              {t(label)}
            </label>
          {/each}
        </fieldset>
      </section>
    {/if}
    {#if pending}
      <div class="scope" role="group" aria-label={t('reader.scope_question')} data-reader-scope>
        <!-- a question that appears must be told: the region is polite, the reader hears it -->
        <p aria-live="polite">{t('reader.scope_question')}</p>
        <button type="button" onclick={() => void answer('device')}>{t('reader.scope_device')}</button>
        <button type="button" onclick={() => void answer('everywhere')}>{t('reader.scope_everywhere')}</button>
        <button type="button" onclick={() => (pending = null)}>{t('reader.cancel')}</button>
      </div>
    {/if}
    {#if help}
      <section class="panel" aria-label={t('reader.help')} data-reader-help>
        <h2>{t('reader.help')}</h2>
        <ul>
          <li>{t('reader.help_turn')}</li>
          <li>{t('reader.help_bounds')}</li>
          <li>{t('reader.help_help')}</li>
          <li>{t('reader.help_escape')}</li>
        </ul>
        <button type="button" onclick={() => (help = false)}>{t('reader.close_help')}</button>
      </section>
    {/if}
  {/if}
</main>

<style>
  .reader {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    padding: 0.75rem;
  }
  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.75rem;
  }
  h1 {
    flex: 1 1 12rem;
    margin: 0;
    font-size: 1.1rem;
  }
  h2 {
    margin: 0;
    font-size: 1rem;
  }
  button {
    min-height: 2.75rem;
    min-width: 2.75rem;
  }
  .badge {
    padding: 0.15rem 0.6rem;
    border: 1px solid var(--color-rule, #ccc);
    border-radius: 1rem;
  }
  .hint,
  .status {
    margin: 0;
    color: var(--color-muted);
  }
  .pages {
    display: flex;
    justify-content: center;
    min-height: 50vh;
  }
  .pages :global([data-reader-page]) {
    display: flex;
    justify-content: center;
    max-height: 85vh;
  }
  .pages :global([data-reader-page] img) {
    max-height: 85vh;
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
  }
  .turning {
    display: flex;
    justify-content: center;
    gap: 0.75rem;
  }
  .panel,
  .scope {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    padding: 0.75rem;
    border: 1px solid var(--color-rule, #ccc);
    border-radius: 0.5rem;
  }
  .panel fieldset {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin: 0;
    border: 0;
    padding: 0;
  }
  .panel label,
  .scope p {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0;
  }
  .scope {
    align-items: center;
  }
  .end {
    text-align: center;
    font-weight: 600;
  }
</style>
