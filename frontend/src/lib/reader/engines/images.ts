// The image engine (ADR-0055, "Port ReaderEngine"): paged left to right or right to left, and the
// continuous webtoon. No iframe, no script of a book: each page is a raster `<img>` fed by a
// `blob:` that this engine made from the bytes of `/content` (ADR-0055, "Images"). Every request
// goes through the link the publication gave, with the session and no referrer, so the content
// route re-checks the profile at each page (ADR-0027). The preload is bounded in pages and in bytes.
import { t } from '#lib/i18n/index.ts';
import type {
  EngineError,
  Locator,
  PublicationEntry,
  PublicationView,
  ReaderEngine,
  ReadingDirection
} from '../engine.ts';

const MIB = 1024 * 1024;
export const PRELOAD_PAGES = 3;
export const PRELOAD_BYTES = 24 * MIB;
const MEDIA_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/avif'] as const;

export interface ImageEngineOptions {
  fetch?: typeof fetch;
  createObjectURL?: (blob: Blob) => string;
  revokeObjectURL?: (url: string) => void;
  /** How many pages ahead of the current one are fetched. */
  preloadPages?: number;
  /** How many bytes of pages are held at most (the page on screen excepted). */
  preloadBytes?: number;
}

export type KeyAction = 'next' | 'previous';

/** What a key does in a paged book: the arrows follow the reading direction, never the numbers. */
export function keyAction(direction: ReadingDirection, key: string): KeyAction | null {
  const forward = direction === 'rtl' ? 'ArrowLeft' : 'ArrowRight';
  const backward = direction === 'rtl' ? 'ArrowRight' : 'ArrowLeft';
  if (key === forward || key === 'PageDown' || key === ' ' || key === 'ArrowDown') return 'next';
  if (key === backward || key === 'PageUp' || key === 'ArrowUp') return 'previous';
  return null;
}

interface Held {
  url: string;
  bytes: number;
}

/** A link of a manifest is kept only if it leads into `/content` of this same instance. */
function contentLink(href: string): boolean {
  try {
    const base = typeof location === 'undefined' ? 'http://localhost/' : location.href;
    const url = new URL(href, base);
    return url.origin === new URL(base).origin && url.pathname.startsWith('/content/');
  } catch {
    return false;
  }
}

export function createImageEngine(options: ImageEngineOptions = {}): ReaderEngine & {
  /** Webtoon: the page that crosses the middle of the screen (the observer's report). */
  reportVisible(index: number): void;
  cachedBytes(): number;
} {
  const fetcher = options.fetch ?? ((...args: Parameters<typeof fetch>) => fetch(...args));
  const makeUrl = options.createObjectURL ?? ((blob: Blob) => URL.createObjectURL(blob));
  const freeUrl = options.revokeObjectURL ?? ((url: string) => URL.revokeObjectURL(url));
  const preloadPages = Math.max(0, options.preloadPages ?? PRELOAD_PAGES);
  const budget = Math.max(0, options.preloadBytes ?? PRELOAD_BYTES);

  const locatorListeners = new Set<(locator: Locator) => void>();
  const endListeners = new Set<() => void>();
  const errorListeners = new Set<(error: EngineError) => void>();

  let pub: PublicationView | null = null;
  let host: HTMLElement | null = null;
  let current = 0;
  let said = -1;
  let ended = false;
  let destroyed = false;
  let held = new Map<number, Held>();
  const asking = new Map<number, Promise<void>>();
  const missing = new Set<number>();
  const images: HTMLImageElement[] = [];
  let aborter = new AbortController();
  let observers: IntersectionObserver[] = [];
  let pageImage: HTMLImageElement | null = null;

  const count = () => pub?.pages.length ?? 0;
  const webtoon = () => pub?.readingDirection === 'webtoon';
  const cachedBytes = () => [...held.values()].reduce((sum, h) => sum + h.bytes, 0);
  const alt = (index: number) => t('reader.page_alt', { n: index + 1, count: count() });

  function say(index: number): void {
    if (index === said) return;
    said = index;
    const locator: Locator = { v: 1, type: 'page', index, count: count() };
    for (const listener of [...locatorListeners]) listener(locator);
  }

  function end(): void {
    if (ended) return;
    ended = true; // said once: the screen puts its sentence in place of a page
    for (const listener of [...endListeners]) listener();
  }

  function fail(error: EngineError): void {
    for (const listener of [...errorListeners]) listener(error);
  }

  function show(index: number): void {
    if (!webtoon() && index !== current) return; // a paged book has one page on screen
    const image = webtoon() ? images[index] : pageImage;
    if (!image || destroyed) return;
    const entry = held.get(index);
    image.alt = alt(index);
    if (entry) {
      if (image.getAttribute('src') !== entry.url) image.src = entry.url;
      image.style.removeProperty('height');
    } else {
      image.removeAttribute('src');
    }
  }

  function evict(index: number): void {
    const entry = held.get(index);
    if (!entry) return;
    const image = webtoon() ? images[index] : null;
    if (image) {
      const height = image.getBoundingClientRect().height;
      if (height > 0) image.style.height = `${height}px`; // the column keeps its length
      image.removeAttribute('src');
    }
    freeUrl(entry.url);
    held.delete(index);
  }

  /** Frees pages until `bytes` more fit: the ones behind the reader first, then (for the page on
   * screen only) the farthest ahead. Says whether there is room. */
  function room(index: number, bytes: number): boolean {
    while (cachedBytes() + bytes > budget) {
      const keys = [...held.keys()].filter((k) => k !== current && k !== index);
      const behind = keys.filter((k) => k < current).sort((a, b) => a - b);
      const ahead = keys.filter((k) => k > current).sort((a, b) => b - a);
      const victim = behind[0] ?? (index === current ? ahead[0] : undefined);
      if (victim === undefined) return false;
      evict(victim);
    }
    return true;
  }

  async function load(entry: PublicationEntry): Promise<void> {
    const { index } = entry;
    if (!contentLink(entry.href)) {
      missing.add(index);
      return;
    }
    try {
      const response = await fetcher(entry.href, {
        credentials: 'same-origin',
        referrerPolicy: 'no-referrer',
        signal: aborter.signal
      });
      if (destroyed) return;
      if (!response.ok) {
        missing.add(index);
        return;
      }
      const declared = (response.headers.get('Content-Type') ?? '').split(';')[0]?.trim().toLowerCase();
      const type = declared || entry.mediaType;
      if (!(MEDIA_TYPES as readonly string[]).includes(type)) {
        missing.add(index);
        return;
      }
      const bytes = await response.arrayBuffer();
      if (destroyed) return;
      if (!room(index, bytes.byteLength) && index !== current) return; // no room for a page ahead
      held.set(index, { url: makeUrl(new Blob([bytes], { type })), bytes: bytes.byteLength });
    } catch {
      if (!destroyed) fail({ kind: 'failed', index });
    }
  }

  function ensure(index: number): Promise<void> {
    if (!pub || index < 0 || index >= count() || held.has(index) || missing.has(index)) {
      return Promise.resolve();
    }
    const running = asking.get(index);
    if (running) return running;
    const entry = pub.pages[index];
    if (!entry) return Promise.resolve();
    const job = load(entry).finally(() => asking.delete(index));
    asking.set(index, job);
    return job;
  }

  /** The page asked for, then the next ones one after the other, so that the bytes in hand are
   * known before the next request and the budget holds. */
  async function fill(): Promise<void> {
    const at = current;
    await ensure(at);
    if (destroyed) return;
    if (at === current) {
      if (missing.has(at)) {
        fail({ kind: 'missing', index: at });
        end(); // an extract ends on its bound; a page the server refuses ends the reading
      } else {
        show(at);
      }
    }
    for (let i = 1; i <= preloadPages; i += 1) {
      if (destroyed || at !== current) return;
      await ensure(at + i);
      if (!held.has(at + i)) return; // refused, or no room: asking further would only be dropped
      if (at === current) show(at + i);
    }
  }

  function moveTo(index: number): void {
    ended = false;
    current = index;
    if (webtoon()) {
      const target = images[index];
      if (target && typeof target.scrollIntoView === 'function') target.scrollIntoView({ block: 'start' });
    } else {
      show(index);
    }
    say(index);
    void fill();
  }

  function mountPaged(root: HTMLElement, direction: ReadingDirection): void {
    const frame = document.createElement('div');
    frame.dataset.readerPage = '';
    frame.dir = direction === 'rtl' ? 'rtl' : 'ltr';
    pageImage = document.createElement('img');
    pageImage.alt = alt(0);
    pageImage.style.maxWidth = '100%';
    pageImage.style.maxHeight = '100%';
    pageImage.decoding = 'async';
    frame.append(pageImage);
    root.append(frame);
  }

  function mountColumn(root: HTMLElement, pages: PublicationEntry[]): void {
    const column = document.createElement('div');
    column.dataset.readerColumn = '';
    column.style.display = 'flex';
    column.style.flexDirection = 'column';
    column.style.alignItems = 'center';
    column.style.gap = '0px'; // pages follow one another with no space (EF-LEC-002)
    for (const page of pages) {
      const image = document.createElement('img');
      image.alt = alt(page.index);
      image.style.display = 'block';
      image.style.width = '100%';
      image.style.maxWidth = '60rem';
      image.decoding = 'async';
      image.dataset.page = String(page.index);
      images.push(image);
      column.append(image);
    }
    root.append(column);
    if (typeof IntersectionObserver === 'undefined') return;
    const near = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const index = Number((entry.target as HTMLElement).dataset.page);
          void ensure(index).then(() => show(index));
        }
      },
      { rootMargin: '1500px 0px' }
    );
    const middle = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) api.reportVisible(Number((entry.target as HTMLElement).dataset.page));
        }
      },
      { rootMargin: '-50% 0px -50% 0px' } // the line across the middle of the screen
    );
    for (const image of images) {
      near.observe(image);
      middle.observe(image);
    }
    observers = [near, middle];
  }

  const api = {
    mediaTypes: MEDIA_TYPES,
    guards: { noIframe: true, rasterOnly: true },

    async open(publication: PublicationView, root: HTMLElement): Promise<void> {
      if (publication.kind !== 'pages' || publication.pages.length === 0) {
        throw new Error('the image engine reads a publication of pages');
      }
      pub = publication;
      host = root;
      current = 0;
      said = -1;
      ended = false;
      destroyed = false;
      aborter = new AbortController();
      held = new Map();
      images.length = 0;
      root.replaceChildren();
      if (publication.readingDirection === 'webtoon') mountColumn(root, publication.pages);
      else mountPaged(root, publication.readingDirection);
      void fill();
    },

    goTo(locator: Locator): void {
      if (!pub || locator.type !== 'page' || locator.count !== count()) return;
      if (!Number.isInteger(locator.index) || locator.index < 0 || locator.index >= count()) return;
      moveTo(locator.index);
    },

    next(): void {
      if (!pub) return;
      if (current + 1 >= count()) return end();
      moveTo(current + 1);
    },

    previous(): void {
      if (!pub || current === 0) return;
      moveTo(current - 1);
    },

    reportVisible(index: number): void {
      if (!pub || !webtoon() || index < 0 || index >= count()) return;
      current = index;
      say(index);
      void fill();
    },

    cachedBytes,

    onLocator(listener: (locator: Locator) => void): () => void {
      locatorListeners.add(listener);
      return () => void locatorListeners.delete(listener);
    },
    onEnd(listener: () => void): () => void {
      endListeners.add(listener);
      return () => void endListeners.delete(listener);
    },
    onError(listener: (error: EngineError) => void): () => void {
      errorListeners.add(listener);
      return () => void errorListeners.delete(listener);
    },

    destroy(): void {
      destroyed = true;
      aborter.abort();
      for (const observer of observers) observer.disconnect();
      observers = [];
      for (const entry of held.values()) freeUrl(entry.url);
      held = new Map();
      asking.clear();
      images.length = 0;
      pageImage = null;
      host?.replaceChildren();
      host = null;
      pub = null;
    }
  };
  return api;
}
