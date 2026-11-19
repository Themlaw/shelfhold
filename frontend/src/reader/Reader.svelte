<script lang="ts">
  // The reader (M5 L10 and L12, EF-LEC-001 to EF-LEC-003, EF-UX-006), mounted by the entry
  // `reader.html`, which is not a route of the SPA (ADR-0055). The screen knows the port
  // `ReaderEngine` and the registry, and nothing of how a page is drawn: an engine is loaded on
  // demand when a book is opened. Every byte comes through the links of the publication, that is
  // through the content route, which asks the profile's visibility again at each page. An extract
  // says so, writes nothing and ends on a sentence. Titles and every word of a book are inserted
  // as text, never as markup (the page itself allows inline styles).
  import { onDestroy, onMount } from 'svelte';
  import { readerPublication, readerPutPosition } from '#lib/api/index.ts';
  import { offlineStore } from '#lib/offline/runtime.ts';
  import { createQueueingPut } from '#lib/offline/sync.ts';
  import type { PositionIn, PublicationOut } from '#lib/api/types.gen.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { readerDeviceId } from '#lib/reader/device.ts';
  import { detectBrowser, type Browser } from '#lib/reader/browser.ts';
  import { checkReaderCsp, type GuardVerdict } from '#lib/reader/csp-guard.ts';
  import {
    keyAction,
    toView,
    type Locator,
    type PublicationView,
    type ReaderEngine
  } from '#lib/reader/engine.ts';
  import { createPositionSender, type PositionSender } from '#lib/reader/positions.ts';
  import { EPUB_TYPE, EngineRefusedError, defaultRegistry } from '#lib/reader/registry.ts';

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
  let offline = $state(false); // read from a copy kept on this device
  let unavailable = $state(false); // no network and no copy
  let engine: ReaderEngine | null = null;
  let sender: PositionSender | null = null;
  let stops: (() => void)[] = [];
  let left = false;
  // Nothing is written before the reader moved: opening a book is not a reading position, and the
  // place another device holds must not be replaced by the first page just because the book opened.
  let moved = false;

  const asksExcerpt = $derived(scope === 'excerpt');
  const isExcerpt = $derived(view?.scope === 'excerpt');
  const direction = $derived(view?.readingDirection ?? 'ltr');
  const status = $derived(
    !at
      ? ''
      : at.type === 'page'
        ? t('reader.status', { n: at.index + 1, count: at.count })
        : t('reader.status_epub', { percent: Math.round(at.totalProgression * 100) })
  );

  const path = () => ({ carrier_type: carrierType, carrier_id: carrierId });

  /** The copy kept for this book by the profile signed in on this browser, if there is one. */
  async function keptCopy(): Promise<{ publication: PublicationOut; profile: string } | null> {
    try {
      const store = offlineStore();
      const profile = await store.activeProfile();
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
    try {
      const got = await readerPublication({
        path: path(),
        query: asksExcerpt ? { scope: 'excerpt' } : undefined
      });
      if (got.response === undefined) throw got.error; // the client returns a failed fetch instead of throwing it
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
              put: async (body) => {
                const { response } = await readerPutPosition({ body, headers: { 'X-CSRF-Token': csrfToken() }, keepalive: true });
                if (response === undefined) throw new TypeError('network'); // a failed fetch is returned, not thrown
                const retry = Number(response.headers.get('Retry-After'));
                return { status: response.status, ...(Number.isFinite(retry) && retry > 0 ? { retryAfter: retry } : {}) };
              }
            })
          : send
    });
    try {
      const registry = defaultRegistry({ browser: browser === undefined ? detectBrowser(navigator) : browser, renew });
      const found = await registry.engineFor(data.kind === 'epub' ? EPUB_TYPE : (view.pages[0]?.mediaType ?? ''));
      if (left || !host) {
        found.destroy();
        return;
      }
      engine = found;
    } catch (e) {
      unsupported = e instanceof EngineRefusedError && e.reason === 'browser' ? 'browser' : 'format';
      return;
    }
    stops = [
      engine.onLocator((locator) => {
        at = locator;
        finished = false;
        if (moved) sender?.push(locator);
      }),
      engine.onEnd(() => (finished = true)),
      engine.onError((e) => (failed = e.kind === 'undisplayable' ? 'display' : 'load'))
    ];
    await engine.open(view, host);
    if (view.kind === 'pages') at = { v: 1, type: 'page', index: 0, count: view.pages.length };
  }

  function turn(action: 'next' | 'previous'): void {
    moved = true;
    if (action === 'next') engine?.next();
    else engine?.previous();
  }

  function onKey(event: KeyboardEvent): void {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    // a key relayed from a document of the book has the window for target, which is not an element
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest('input, textarea, select')) return;
    if (event.key === 'Escape') {
      // the focus is never trapped: Escape gives it back to the controls
      toolbar?.querySelector<HTMLElement>('button, a')?.focus();
      return;
    }
    if (!view || view.readingDirection === 'webtoon') return; // a column scrolls by itself
    const action = keyAction(view.readingDirection, event.key);
    if (action === null) return;
    if (target?.closest('button, a') && event.key === ' ') return; // a focused control keeps its key
    event.preventDefault();
    turn(action);
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
    <p role="alert">{t('reader.offline_missing')}</p>
  {:else if problem}
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
      {#if offline}<span class="badge">{t('reader.offline_badge')}</span>{/if}
      <button
        type="button"
        onclick={() => turn('previous')}
        aria-disabled={!at || (at.type === 'page' && at.index === 0)}>{t('reader.previous')}</button
      >
      <button type="button" onclick={() => turn('next')} aria-disabled={!at}>{t('reader.next')}</button
      >
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
    ></div>
    {#if finished}
      <p class="end" data-reader-end>
        {isExcerpt ? t('reader.end_of_excerpt') : t('reader.end_of_book')}
      </p>
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
  .pages :global([data-reader-column]) {
    width: 100%;
  }
  .end {
    text-align: center;
    font-weight: 600;
  }
</style>
