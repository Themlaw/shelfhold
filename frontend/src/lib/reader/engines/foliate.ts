// The EPUB engine on foliate-js (ADR-0055, "EPUB engine", docs/dependencies.md, "consigne pour
// L12"). This file is the only one that imports foliate (ESLint holds the line). What it does that
// the library would not:
// - it builds the book object itself, from the links of the publication, so that foliate never
//   sees a `File`, a `Blob` or a string and never picks a format on the content (a PDF, a MOBI or
//   an FB2 dropped as an EPUB would be drawn by a path with no filter);
// - it installs the content filter on the loader before the first read;
// - it always cancels the `external-link` of foliate (which would open the address with no
//   `noopener`), asks, then opens the address apart;
// - it moves no node of a book, never clones or imports one: the keys it relays are events of its own.
import { EPUB } from '../../../../vendor/foliate-js/epub.js';
import '../../../../vendor/foliate-js/view.js'; // defines <foliate-view>; its dynamic imports are stubbed by the build of the reader
import { t } from '#lib/i18n/index.ts';
import type { CfiLocator, EngineError, Locator, PublicationView, ReaderEngine } from '../engine.ts';
import { CONTENT_FILTER, FilterRefusal, installFilter } from '../filter.ts';
import { EPUB_TYPE } from '../registry.ts';

export interface FoliateViewLike extends EventTarget {
  open(book: unknown): Promise<void>;
  init?(options: { lastLocation?: string; showTextStart?: boolean }): Promise<void>;
  goTo(target: unknown): Promise<unknown>;
  next(): Promise<unknown>;
  prev(): Promise<unknown>;
  close?(): void;
  remove(): void;
}

export interface EpubEngineOptions {
  fetch?: typeof fetch;
  createView?: () => FoliateViewLike;
  /** The origin of the instance, for the `blob:` of the loader (default: `location.origin`). */
  origin?: string;
  /** Asks the reader before an address is opened apart; `false` opens nothing. */
  confirmExternal?: (href: string) => Promise<boolean>;
  openExternal?: (href: string) => void;
  /** New links for the same book (a ticket lives 10 minutes); `null`: nothing more to give. */
  renew?: () => Promise<PublicationView | null>;
  /** The clock, in milliseconds (default `Date.now`). */
  now?: () => number;
}

/** What the adapter uses of `EPUB` (foliate is plain JavaScript: this is its typing, here and nowhere else). */
interface EpubBook {
  init(): Promise<unknown>;
  sections: { load: () => Promise<unknown> }[];
  transformTarget: EventTarget;
  destroy(): void;
}
interface EpubLoader {
  loadText(name: string): Promise<string | null>;
  loadBlob(name: string): Promise<Blob | null>;
  getSize(name: string): number;
  sha1: undefined;
}

/** The ticket ran out and no new one could be had: not a section that is missing. */
class TicketError extends Error {
  constructor() {
    super('ticket');
  }
}

/** A ticket is renewed this long before the clock ends it. */
const TICKET_SKEW_MS = 5_000;

class MissingError extends Error {
  constructor(readonly entry: string) {
    super('missing');
  }
}

const CONTAINER = (opf: string) =>
  `<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="${opf
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')}" media-type="application/oebps-package+xml"/></rootfiles></container>`;

const CFI = /^epubcfi\(.{1,2000}\)$/s;
const clamp = (value: unknown): number => (typeof value === 'number' && Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0);

/** `/content/<ticket>/<entry>` → the part up to and including the ticket. */
function ticketBase(link: string): { base: string; entry: string } {
  const match = /^(\/content\/[^/]+\/)(.+)$/.exec(link.split(/[?#]/)[0] ?? '');
  if (!match) throw new Error('the package of the publication is not a link of the content route');
  return { base: match[1]!, entry: match[2]!.split('/').map(decodeURIComponent).join('/') };
}

/** A name of the book that may be asked of the server: relative, no climb, no scheme, no backslash. */
function safeName(name: unknown): name is string {
  if (typeof name !== 'string' || name === '' || name.length > 1024) return false;
  if (name.startsWith('/') || name.includes('\\') || name.includes('\0') || /^[a-z][a-z0-9+.-]*:/i.test(name)) return false;
  return name.split('/').every((part) => part !== '..' && part !== '.' && part !== '');
}

const isHttp = (href: unknown): href is string => {
  if (typeof href !== 'string') return false;
  if (!/^https?:\/\//i.test(href)) return false; // no leading space, no tab inside the scheme
  try {
    const url = new URL(href);
    // no `user:password@host`: the text of a confirmation must not be made to show another host
    return (url.protocol === 'http:' || url.protocol === 'https:') && url.username === '' && url.password === '';
  } catch {
    return false;
  }
};

export function createEpubEngine(options: EpubEngineOptions = {}): ReaderEngine {
  const doFetch = options.fetch ?? ((input, init) => fetch(input, init));
  const locatorListeners = new Set<(locator: Locator) => void>();
  const endListeners = new Set<() => void>();
  const errorListeners = new Set<(error: EngineError) => void>();
  let view: FoliateViewLike | null = null;
  let book: EpubBook | null = null;
  let stops: (() => void)[] = [];
  let ended = false;
  let destroyed = false;

  const say = <T>(listeners: Set<(value: T) => void>, value: T) => {
    for (const listener of [...listeners]) listener(value);
  };

  return {
    mediaTypes: [EPUB_TYPE],
    guards: { contentFilter: true },

    async open(publication, host) {
      if (publication.kind !== 'epub' || publication.package === null) throw new Error('not an EPUB publication');
      const origin = options.origin ?? location.origin;
      const first = ticketBase(publication.package);
      const opf = first.entry;
      let base = first.base;
      let expiresAt = publication.expiresAt ?? null;
      const now = options.now ?? Date.now;
      const expired = () => expiresAt !== null && now() >= expiresAt - TICKET_SKEW_MS;

      // One renewal at a time, shared: the requests that find the ticket gone together wait for the
      // same new one (a flag shared between them made the second one read a stale ticket and
      // report a section that is there as missing).
      let renewing: Promise<boolean> | null = null;
      const renewTickets = (): Promise<boolean> => {
        if (renewing) return renewing;
        const run = (async () => {
          try {
            const fresh = options.renew ? await options.renew() : null;
            if (!fresh?.package) return false;
            base = ticketBase(fresh.package).base;
            expiresAt = fresh.expiresAt ?? null;
            return true;
          } catch {
            return false;
          }
        })();
        renewing = run;
        void run.finally(() => {
          if (renewing === run) renewing = null;
        });
        return run;
      };

      const get = async (name: string): Promise<Response> => {
        const ask = (from: string) =>
          doFetch(`${from}${name.split('/').map(encodeURIComponent).join('/')}`, {
            credentials: 'same-origin',
            referrerPolicy: 'no-referrer',
            redirect: 'error'
          });
        // a ticket that the clock says is gone is renewed before the request, not after a 404
        if (expired()) await renewTickets();
        const used = base;
        let response = await ask(used);
        if (!response.ok) {
          if (used !== base) {
            response = await ask(base); // another request renewed the ticket meanwhile
          } else if (options.renew && (publication.scope === 'full' || expired())) {
            // The route answers 404 for a gone ticket as for an absent entry: in a full book that
            // is a ticket until a new one has said otherwise; in an excerpt, an entry outside the
            // cut is the same answer and must not call the API (only the clock says "expired").
            if (!(await renewTickets())) throw new TicketError();
            response = await ask(base);
          }
        }
        if (!response.ok) throw new MissingError(name);
        return response;
      };

      const epub = new (EPUB as unknown as new (loader: EpubLoader) => EpubBook)({
        loadText: async (name: string) => {
          if (name === 'META-INF/container.xml') return CONTAINER(opf);
          if (name.startsWith('META-INF/')) return null; // nothing is obfuscated, no display options: nothing is asked
          if (!safeName(name)) return null;
          return (await get(name)).text();
        },
        loadBlob: async (name: string) => {
          if (!safeName(name)) return null;
          return (await get(name)).blob();
        },
        // the sizes are not known without a request each: the sections weigh the same
        getSize: () => 1,
        sha1: undefined
      });
      await epub.init();
      // before the first read, and before foliate sees the book: no read has gone through the loader yet
      installFilter(epub.transformTarget, { origin });

      const fail = (index: number, cause: unknown) => {
        if (destroyed) return;
        if (cause instanceof MissingError) {
          if (publication.scope === 'excerpt') {
            if (!ended) {
              ended = true;
              say(endListeners, undefined as never);
            }
            return;
          }
          say(errorListeners, { kind: 'missing', index });
        } else if (cause instanceof FilterRefusal) say(errorListeners, { kind: 'undisplayable', index });
        else say(errorListeners, { kind: 'failed', index });
      };
      epub.sections = epub.sections.map((section, index) => ({
        ...section,
        load: async () => {
          try {
            const url = await section.load();
            if (!url) throw new FilterRefusal('the section is not one the filter lets through');
            return url;
          } catch (cause) {
            fail(index, cause);
            throw cause;
          }
        }
      }));
      book = epub;

      const el = options.createView ? options.createView() : (document.createElement('foliate-view') as unknown as FoliateViewLike);
      view = el;
      const onRelocate = (event: Event) => {
        const detail = (event as CustomEvent<{ cfi?: unknown; fraction?: unknown }>).detail;
        if (typeof detail?.cfi !== 'string' || !CFI.test(detail.cfi)) return;
        const locator: CfiLocator = { v: 1, type: 'cfi', cfi: detail.cfi, totalProgression: clamp(detail.fraction), filter: CONTENT_FILTER };
        ended = false;
        say(locatorListeners, locator);
      };
      const onExternal = (event: Event) => {
        event.preventDefault(); // foliate would open it with no `noopener`; this is never a navigation of the frame
        const href = (event as CustomEvent<{ href_?: unknown }>).detail?.href_;
        if (!isHttp(href)) return;
        const ask = options.confirmExternal ?? (async (address: string) => window.confirm(t('reader.external_confirm', { href: new URL(address).host })));
        const open = options.openExternal ?? ((address: string) => void window.open(address, '_blank', 'noopener,noreferrer'));
        void ask(href).then((yes) => {
          if (yes && !destroyed) open(href);
        });
      };
      // the keys of a document of the book are the reader's: the host sees them, as events of its own
      const onLoad = (event: Event) => {
        const doc = (event as CustomEvent<{ doc?: Document }>).detail?.doc;
        doc?.addEventListener('keydown', (key) => {
          window.dispatchEvent(
            new KeyboardEvent('keydown', { key: key.key, code: key.code, altKey: key.altKey, ctrlKey: key.ctrlKey, metaKey: key.metaKey, shiftKey: key.shiftKey, bubbles: true, cancelable: true })
          );
        });
      };
      el.addEventListener('relocate', onRelocate);
      el.addEventListener('external-link', onExternal);
      el.addEventListener('load', onLoad);
      stops = [
        () => el.removeEventListener('relocate', onRelocate),
        () => el.removeEventListener('external-link', onExternal),
        () => el.removeEventListener('load', onLoad)
      ];
      if (el instanceof HTMLElement) {
        el.style.cssText = 'display:block;width:100%;height:85vh';
        el.setAttribute('role', 'document');
        if (publication.title) el.setAttribute('aria-label', publication.title);
        host.append(el);
      }
      // an object, never a File, a Blob or a string: foliate picks no format here
      await el.open(epub);
      await el.init?.({});
    },

    goTo(locator) {
      if (locator.type !== 'cfi' || !CFI.test(locator.cfi)) return;
      void view?.goTo(locator.cfi);
    },
    next() {
      void view?.next();
    },
    previous() {
      void view?.prev();
    },
    onLocator(listener) {
      locatorListeners.add(listener);
      return () => void locatorListeners.delete(listener);
    },
    onEnd(listener) {
      endListeners.add(listener);
      return () => void endListeners.delete(listener);
    },
    onError(listener) {
      errorListeners.add(listener);
      return () => void errorListeners.delete(listener);
    },
    destroy() {
      destroyed = true;
      for (const stop of stops) stop();
      stops = [];
      view?.close?.();
      view?.remove();
      book?.destroy(); // every `blob:` of the book is let go
      view = null;
      book = null;
      locatorListeners.clear();
      endListeners.clear();
      errorListeners.clear();
    }
  };
}
