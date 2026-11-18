<script lang="ts">
  // The reader of images (M5 L10, EF-LEC-001, EF-LEC-002, EF-UX-006). The screen knows the port
  // `ReaderEngine` and nothing of how a page is drawn: the engine is loaded on demand when a book
  // is opened (ADR-0013), so the rest of the app does not carry it. Every byte comes through the
  // links of the publication, that is through the content route, which asks the profile's
  // visibility again at each page. An extract says so, writes nothing and ends on a sentence.
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import { readerPublication, readerPutPosition } from '#lib/api/index.ts';
  import ProblemCard from '#lib/components/ProblemCard.svelte';
  import { t } from '#lib/i18n/index.ts';
  import { csrfToken } from '#lib/passkeys.ts';
  import { problemView, type ProblemView } from '#lib/problems.ts';
  import { readerDeviceId } from '#lib/reader/device.ts';
  import {
    keyAction,
    toView,
    type Locator,
    type PublicationView,
    type ReaderEngine
  } from '#lib/reader/engine.ts';
  import { createPositionSender, type PositionSender } from '#lib/reader/positions.ts';

  let host = $state<HTMLElement | null>(null);
  let toolbar = $state<HTMLElement | null>(null);
  let problem = $state<ProblemView | null>(null);
  let unsupported = $state(false);
  let view = $state<PublicationView | null>(null);
  let at = $state<Locator | null>(null);
  let finished = $state(false);
  let failed = $state(false);
  let engine: ReaderEngine | null = null;
  let sender: PositionSender | null = null;
  let stops: (() => void)[] = [];
  let left = false;
  // Nothing is written before the reader moved: opening a book is not a reading position, and the
  // place another device holds must not be replaced by the first page just because the book opened.
  let moved = false;

  const asksExcerpt = $derived(page.url.searchParams.get('scope') === 'excerpt');
  const isExcerpt = $derived(view?.scope === 'excerpt');
  const direction = $derived(view?.readingDirection ?? 'ltr');
  const status = $derived(at ? t('reader.status', { n: at.index + 1, count: at.count }) : '');

  const path = () => ({
    carrier_type: page.params.carrier_type ?? '',
    carrier_id: page.params.carrier_id ?? ''
  });

  /** The same book again, with new tickets and the same scope: a reading outlives a ticket. */
  async function renew(): Promise<PublicationView | null> {
    const { data } = await readerPublication({ path: path(), query: { scope: view?.scope ?? 'full' } });
    return data && data.kind === 'pages' ? toView(data) : null;
  }

  async function open(): Promise<void> {
    const { data, error } = await readerPublication({
      path: path(),
      query: asksExcerpt ? { scope: 'excerpt' } : undefined
    });
    if (left) return;
    if (error !== undefined || !data) {
      problem = problemView(error);
      return;
    }
    if (data.kind !== 'pages') {
      unsupported = true; // the EPUB engine comes with L12
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
    const { createImageEngine } = await import('#lib/reader/engines/images.ts');
    if (left || !host) return;
    engine = createImageEngine({ renew });
    stops = [
      engine.onLocator((locator) => {
        at = locator;
        finished = false;
        if (moved) sender?.push(locator);
      }),
      engine.onEnd(() => (finished = true)),
      engine.onError(() => (failed = true))
    ];
    await engine.open(view, host);
    at = { v: 1, type: 'page', index: 0, count: view.pages.length };
  }

  function turn(action: 'next' | 'previous'): void {
    moved = true;
    if (action === 'next') engine?.next();
    else engine?.previous();
  }

  function onKey(event: KeyboardEvent): void {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const target = event.target as HTMLElement | null;
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
  {#if problem}
    <ProblemCard {problem} level={1} />
  {:else if unsupported}
    <p role="alert">{t('reader.unsupported')}</p>
  {:else}
    <header bind:this={toolbar} role="group" aria-label={t('reader.toolbar')}>
      <button type="button" onclick={() => history.back()}>{t('reader.close')}</button>
      <h1>{view?.title ?? t('reader.loading')}</h1>
      {#if isExcerpt}<span class="badge">{t('reader.excerpt_badge')}</span>{/if}
      <button
        type="button"
        onclick={() => turn('previous')}
        aria-disabled={!at || at.index === 0}>{t('reader.previous')}</button
      >
      <button type="button" onclick={() => turn('next')} aria-disabled={!at}>{t('reader.next')}</button
      >
    </header>
    <p class="hint">{t('reader.keys')}</p>
    <p role="status" class="status">{status}</p>
    {#if failed}<p class="notice">{t('reader.page_failed')}</p>{/if}
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
