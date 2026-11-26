// The EPUB adapter (ADR-0055, "EPUB engine", docs/dependencies.md, "the L12 instruction"): the one
// place foliate-js is imported. It builds the book object itself from the filtered route of the
// content, installs the filter before anything is read, and never gives foliate a file.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CONTENT_FILTER } from '../filter.ts';
import type { Locator, PublicationView } from '../engine.ts';
import { createEpubEngine, type FoliateViewLike } from './foliate.ts';

const NS = 'http://www.w3.org/1999/xhtml';
const TICKET = '/content/TKT';
const OPF = `<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="i"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="i">x</dc:identifier><dc:title>T</dc:title><dc:language>fr</dc:language></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/><item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/><item id="js" href="x.js" media-type="text/javascript"/><item id="img" href="p.svg" media-type="image/png"/></manifest><spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>`;
const NAV = `<?xml version="1.0"?><html xmlns="${NS}" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>n</title></head><body><nav epub:type="toc"><ol><li><a href="c1.xhtml">Un</a></li></ol></nav></body></html>`;
const C1 = `<?xml version="1.0"?><html xmlns="${NS}"><head><title>c</title><script src="x.js"></script></head><body><p id="a">t</p><script>fetch('/api')</script><img src="p.svg"/><a href="https://example.org/">e</a></body></html>`;

const files: Record<string, string | null> = {
  'OEBPS/content.opf': OPF,
  'OEBPS/nav.xhtml': NAV,
  'OEBPS/c1.xhtml': C1,
  'OEBPS/c2.xhtml': null,
  'OEBPS/x.js': 'alert(1)',
  'OEBPS/p.svg': `<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><rect width="1" height="1"/></svg>`
};

const view = (over: Partial<PublicationView> = {}): PublicationView => ({
  kind: 'epub',
  scope: 'full',
  readingDirection: 'ltr',
  title: 'T',
  language: 'fr',
  pages: [],
  spine: [
    { index: 0, href: `${TICKET}/OEBPS/c1.xhtml`, mediaType: 'application/xhtml+xml' },
    { index: 1, href: `${TICKET}/OEBPS/c2.xhtml`, mediaType: 'application/xhtml+xml' }
  ],
  package: `${TICKET}/OEBPS/content.opf`,
  seriesId: '01HZZZZZZZZZZZZZZZZZZZZZZW', // the Œuvre the manifest names since L13
  ...over
});

class FakeView extends EventTarget implements FoliateViewLike {
  opened: unknown[] = [];
  went: unknown[] = [];
  closed = false;
  renderer = { setStyles: vi.fn(), setAttribute: vi.fn() };
  async open(book: unknown) {
    this.opened.push(book);
  }
  async goTo(target: unknown) {
    this.went.push(target);
  }
  async next() {}
  async prev() {}
  close() {
    this.closed = true;
  }
  remove() {}
}

let requested: { url: string; init?: RequestInit }[] = [];
const blobs = new Map<string, Blob>();
let counter = 0;

beforeEach(() => {
  requested = [];
  blobs.clear();
  vi.stubGlobal('URL', Object.assign(URL, {
    createObjectURL: (b: Blob) => {
      const url = `blob:https://shelfhold.test/${(counter += 1)}`;
      blobs.set(url, b);
      return url;
    },
    revokeObjectURL: (u: string) => void blobs.delete(u)
  }));
});
afterEach(() => vi.unstubAllGlobals());

const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input);
  requested.push({ url, init });
  const name = url.replace(`${TICKET}/`, '').split('?')[0]!;
  const body = files[decodeURIComponent(name)];
  if (body === undefined || body === null) return new Response('no', { status: 404 });
  return new Response(body, { status: 200 });
}) as typeof fetch;

interface BookLike {
  sections: { load(): Promise<unknown> }[];
  loadBlob(name: string): Promise<unknown>;
  loadText(name: string): Promise<unknown>;
  transformTarget: unknown;
  isDirectory?: unknown;
  arrayBuffer?: unknown;
}

async function opened(opts: Partial<Parameters<typeof createEpubEngine>[0]> = {}, v: PublicationView = view()) {
  const fake = new FakeView();
  const engine = createEpubEngine({ fetch: fetcher, createView: () => fake, origin: 'https://shelfhold.test', ...opts });
  const host = document.createElement('div');
  await engine.open(v, host);
  return { engine, fake, host, book: fake.opened[0] as BookLike };
}

describe('the EPUB engine', () => {
  // req: EF-LEC-003
  it('declares its type and its guard', () => {
    const engine = createEpubEngine({ fetch: fetcher });
    expect(engine.mediaTypes).toEqual(['application/epub+zip']);
    expect(engine.guards).toEqual({ contentFilter: true });
  });

  // req: ENF-SEC-004
  it('hands foliate a book object it built itself, never a File, a Blob or a string', async () => {
    const { fake, book } = await opened();
    expect(fake.opened).toHaveLength(1);
    expect(typeof book).toBe('object');
    expect(book).not.toBeInstanceOf(Blob);
    expect(typeof book.arrayBuffer).not.toBe('function');
    expect(book.isDirectory).toBeFalsy();
    expect(Array.isArray(book.sections)).toBe(true);
    expect(book.sections).toHaveLength(2);
    expect(book.transformTarget).toBeInstanceOf(EventTarget);
  });

  // req: EF-LEC-003
  it('gives the book the paper and the ink of the page around it, and nothing when the theme has none', async () => {
    const root = document.documentElement;
    root.style.setProperty('--color-bg', '#191714');
    root.style.setProperty('--color-ink', '#ECE5D6');
    const { fake } = await opened();
    const css = (fake.renderer.setStyles.mock.calls[0] as [string])[0];
    expect(css).toContain('background: #191714');
    expect(css).toContain('color: #ECE5D6');
    root.style.removeProperty('--color-bg');
    root.style.removeProperty('--color-ink');
    const bare = await opened();
    expect((bare.fake.renderer.setStyles.mock.calls[0] as [string])[0]).toBe('');
  });

  // req: ENF-SEC-004
  it('refuses a publication that is not an EPUB, and one with no package', async () => {
    const engine = createEpubEngine({ fetch: fetcher, createView: () => new FakeView() });
    await expect(engine.open(view({ kind: 'pages' }), document.createElement('div'))).rejects.toThrow();
    await expect(engine.open(view({ package: null }), document.createElement('div'))).rejects.toThrow();
  });

  // req: ENF-SEC-004
  it('reads only through the links of the publication: same session, no referrer, the ticket of the package', async () => {
    await opened();
    expect(requested.length).toBeGreaterThan(0);
    for (const { url, init } of requested) {
      expect(url.startsWith(`${TICKET}/`), url).toBe(true);
      expect(init?.referrerPolicy).toBe('no-referrer');
      expect(init?.credentials).toBe('same-origin');
    }
    // the container and the encryption file are made here, not asked of the server
    expect(requested.map((r) => r.url)).not.toContain(`${TICKET}/META-INF/container.xml`);
    expect(requested.map((r) => r.url)).not.toContain(`${TICKET}/META-INF/encryption.xml`);
    expect(requested.map((r) => r.url)).toContain(`${TICKET}/OEBPS/content.opf`);
  });

  // req: ENF-SEC-004
  it('cannot be made to read a name that climbs or leaves the ticket', async () => {
    const { book } = await opened();
    await expect(book.loadBlob('../../etc/passwd')).resolves.toBeNull();
    await expect(book.loadText('https://evil.example/x')).resolves.toBeNull();
    await expect(book.loadText('/api/v1/keys')).resolves.toBeNull();
    expect(requested.some((r) => /etc\/passwd|evil|\/api/.test(r.url))).toBe(false);
  });

  // req: ENF-SEC-004
  it('test_epub_script_never_runs (unit): what the loader makes into a blob has passed the filter', async () => {
    const { book } = await opened();
    const url = (await book.sections[0]!.load()) as string;
    const doc = new DOMParser().parseFromString(await blobs.get(url)!.text(), 'application/xhtml+xml');
    expect(doc.querySelector('script')).toBeNull();
    expect(doc.querySelector('#a')?.textContent).toBe('t');
    expect(blobs.get(url)!.type).toBe('application/xhtml+xml');
    // the image the OPF called a PNG is an SVG by its name: filtered, typed as an SVG
    const img = doc.querySelector('img')!.getAttribute('src')!;
    expect(img.startsWith('blob:')).toBe(true);
    // the OPF called it a PNG, its name says SVG: it is never drawn as a document
    expect(blobs.get(img)!.type).toBe('application/octet-stream');
    // the script the OPF declared is never loaded
    expect([...blobs.values()].some((b) => /alert\(1\)$/.test(String(b.size)))).toBe(false);
    expect(requested.map((r) => r.url)).not.toContain(`${TICKET}/OEBPS/x.js`);
  });

  // req: EF-LEC-003
  it('says a position as a locator v1, with the version of the filter', async () => {
    const { engine, fake } = await opened();
    const seen: Locator[] = [];
    engine.onLocator((l) => seen.push(l));
    fake.dispatchEvent(new CustomEvent('relocate', { detail: { cfi: 'epubcfi(/6/4!/4/2/1:0)', fraction: 0.37 } }));
    fake.dispatchEvent(new CustomEvent('relocate', { detail: { cfi: 'epubcfi(/6/4!/4/2/1:0)', fraction: 7 } }));
    fake.dispatchEvent(new CustomEvent('relocate', { detail: { cfi: 'not a cfi', fraction: 0.5 } }));
    fake.dispatchEvent(new CustomEvent('relocate', { detail: { cfi: 'epubcfi(/6/4)', fraction: Number.NaN } }));
    expect(seen).toEqual([
      { v: 1, type: 'cfi', cfi: 'epubcfi(/6/4!/4/2/1:0)', totalProgression: 0.37, filter: CONTENT_FILTER },
      { v: 1, type: 'cfi', cfi: 'epubcfi(/6/4!/4/2/1:0)', totalProgression: 1, filter: CONTENT_FILTER },
      { v: 1, type: 'cfi', cfi: 'epubcfi(/6/4)', totalProgression: 0, filter: CONTENT_FILTER }
    ]);
  });

  // req: EF-LEC-003
  it('goes to a stored position by its CFI, and ignores a locator that is not a CFI', async () => {
    const { engine, fake } = await opened();
    engine.goTo({ v: 1, type: 'cfi', cfi: 'epubcfi(/6/2)', totalProgression: 0.1, filter: CONTENT_FILTER });
    engine.goTo({ v: 1, type: 'page', index: 1, count: 4 });
    engine.goTo({ v: 1, type: 'cfi', cfi: 'javascript:alert(1)', totalProgression: 0.1, filter: CONTENT_FILTER });
    expect(fake.went).toEqual(['epubcfi(/6/2)']);
  });

  // req: ENF-SEC-004
  it('test_external_link_never_navigates_iframe: the event is always cancelled, then confirmed, then opened apart', async () => {
    const confirmExternal = vi.fn(async () => true);
    const openExternal = vi.fn();
    const { fake } = await opened({ confirmExternal, openExternal });
    const event = new CustomEvent('external-link', { detail: { a: document.createElement('a'), href_: 'https://example.org/page' }, cancelable: true });
    fake.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true); // foliate would open it without noopener
    await vi.waitFor(() => expect(openExternal).toHaveBeenCalledWith('https://example.org/page'));
    expect(confirmExternal).toHaveBeenCalledWith('https://example.org/page');
  });

  // req: ENF-SEC-004
  it('opens nothing when the reader says no, and nothing that is not http(s)', async () => {
    const openExternal = vi.fn();
    const no = await opened({ confirmExternal: async () => false, openExternal });
    const refused = new CustomEvent('external-link', { detail: { a: document.createElement('a'), href_: 'https://example.org/' }, cancelable: true });
    no.fake.dispatchEvent(refused);
    const yes = await opened({ confirmExternal: async () => true, openExternal });
    for (const href of ['https://shelfhold.example@evil.example/', 'javascript:alert(1)', 'data:text/html,x', 'blob:https://shelfhold.test/1', ' JaVa\tScript:1', 'file:///etc/passwd', '//evil.example']) {
      const event = new CustomEvent('external-link', { detail: { a: document.createElement('a'), href_: href }, cancelable: true });
      yes.fake.dispatchEvent(event);
      expect(event.defaultPrevented, href).toBe(true);
    }
    await new Promise((r) => setTimeout(r, 20));
    expect(openExternal).not.toHaveBeenCalled();
  });

  // req: ENF-SEC-004
  it('opens an address apart with noopener and noreferrer by default', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    const { fake } = await opened({ confirmExternal: async () => true });
    fake.dispatchEvent(new CustomEvent('external-link', { detail: { a: document.createElement('a'), href_: 'https://example.org/' }, cancelable: true }));
    await vi.waitFor(() => expect(open).toHaveBeenCalledWith('https://example.org/', '_blank', 'noopener,noreferrer'));
    open.mockRestore();
  });

  // req: EF-LEC-005
  it('says the end of an extract once, and a missing page of a full book as an error', async () => {
    const extract = await opened({}, view({ scope: 'excerpt' }));
    const ends = vi.fn();
    extract.engine.onEnd(ends);
    await expect(extract.book.sections[1]!.load()).rejects.toBeDefined();
    expect(ends).toHaveBeenCalledTimes(1);

    const full = await opened();
    const errors: unknown[] = [];
    full.engine.onError((e) => errors.push(e));
    await expect(full.book.sections[1]!.load()).rejects.toBeDefined();
    expect(errors).toEqual([{ kind: 'missing', index: 1 }]);
  });

  // req: ENF-SEC-004
  it('lets go of everything on destroy: the view, the blobs, the listeners', async () => {
    const { engine, fake, book } = await opened();
    await book.sections[0]!.load();
    expect(blobs.size).toBeGreaterThan(0);
    const seen: Locator[] = [];
    engine.onLocator((l) => seen.push(l));
    engine.destroy();
    expect(fake.closed).toBe(true);
    expect(blobs.size).toBe(0);
    fake.dispatchEvent(new CustomEvent('relocate', { detail: { cfi: 'epubcfi(/6/2)', fraction: 0.5 } }));
    expect(seen).toEqual([]);
  });
});

describe('the tickets of the engine (#381 C2)', () => {
  const NEW = '/content/NEW';
  // the route answers 404 for a gone ticket as for an absent entry
  const served = (live: string) =>
    (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      requested.push({ url, init });
      if (!url.startsWith(`${live}/`)) return new Response('no', { status: 404 });
      const body = files[decodeURIComponent(url.slice(live.length + 1).split('?')[0]!)];
      return body == null ? new Response('no', { status: 404 }) : new Response(body, { status: 200 });
    }) as typeof fetch;
  const fresh = (): PublicationView =>
    view({ package: `${NEW}/OEBPS/content.opf`, spine: [], expiresAt: Date.now() + 600_000 });
  const outcomes = (engine: ReturnType<typeof createEpubEngine>) => {
    const errors: { kind: string; index: number }[] = [];
    const ends: number[] = [];
    engine.onError((e) => errors.push(e));
    engine.onEnd(() => ends.push(1));
    return { errors, ends };
  };

  // req: EF-LEC-003
  it('renews a ticket that the clock has ended once for all the requests that find it gone, and shows no missing section', async () => {
    const renew = vi.fn(async () => fresh());
    let clock = 1_000;
    let live = '/content/TKT';
    const route = (async (input: RequestInfo | URL, init?: RequestInit) => served(live)(input, init)) as typeof fetch;
    const { engine, book } = await opened({ renew, now: () => clock, fetch: route }, view({ expiresAt: 100_000 }));
    const { errors } = outcomes(engine);
    clock = 200_000;
    live = NEW; // what the server does once the first ticket is gone
    const reads = await Promise.all(['OEBPS/nav.xhtml', 'OEBPS/c1.xhtml', 'OEBPS/content.opf'].map((name) => book.loadText(name)));
    expect(renew).toHaveBeenCalledTimes(1);
    expect(errors).toEqual([]);
    expect(reads.every((r) => typeof r === 'string' && r.length > 0)).toBe(true);
  });

  // req: EF-LEC-003
  it('a request that found the old ticket gone while another one renewed it asks again with the new one, not a missing section', async () => {
    let live = '/content/TKT';
    const renew = vi.fn(async () => {
      live = NEW;
      return fresh();
    });
    const route = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const response = await served(live)(input, init);
      return response;
    }) as typeof fetch;
    const { engine, book } = await opened({ renew, fetch: route });
    const { errors } = outcomes(engine);
    live = NEW; // the first ticket is gone for everyone, the clock does not know it
    const reads = await Promise.all(['OEBPS/nav.xhtml', 'OEBPS/c1.xhtml'].map((name) => book.loadText(name)));
    expect(renew).toHaveBeenCalledTimes(1);
    expect(errors).toEqual([]);
    expect(reads.every((r) => typeof r === 'string')).toBe(true);
  });

  // req: EF-LEC-003
  it('in a full book, a 404 under a ticket the clock calls live asks for a new one once, then says missing if it is still not there', async () => {
    const renew = vi.fn(async () => view({ package: `/content/TKT/OEBPS/content.opf`, spine: [] }));
    const { engine, book } = await opened({ renew, fetch: served('/content/TKT') });
    const { errors } = outcomes(engine);
    await expect(book.sections[1]!.load()).rejects.toBeDefined();
    expect(renew).toHaveBeenCalledTimes(1);
    expect(errors).toEqual([{ kind: 'missing', index: 1 }]);
  });

  // req: EF-LEC-003
  it('a ticket that cannot be renewed is a failure of the reading, never a section that is missing', async () => {
    const renew = vi.fn(async () => null);
    const { engine, book } = await opened({ renew, fetch: served('/content/TKT') });
    const { errors } = outcomes(engine);
    await expect(book.sections[1]!.load()).rejects.toBeDefined();
    expect(errors).toEqual([{ kind: 'failed', index: 1 }]);
  });

  // req: EF-LEC-003
  it('in an excerpt, a section outside the cut ends the reading without a call to the API', async () => {
    const renew = vi.fn(async () => fresh());
    const { engine, book } = await opened({ renew, fetch: served('/content/TKT') }, view({ scope: 'excerpt', expiresAt: Date.now() + 600_000 }));
    const { errors, ends } = outcomes(engine);
    await expect(book.sections[1]!.load()).rejects.toBeDefined();
    expect(renew).not.toHaveBeenCalled();
    expect(errors).toEqual([]);
    expect(ends).toEqual([1]);
  });

  // req: EF-LEC-003
  it('in an excerpt too, a ticket the clock has ended is renewed', async () => {
    let clock = 1_000;
    const renew = vi.fn(async () => view({ package: `/content/TKT/OEBPS/content.opf`, spine: [], scope: 'excerpt', expiresAt: 900_000 }));
    const { book } = await opened({ renew, now: () => clock, fetch: served('/content/TKT') }, view({ scope: 'excerpt', expiresAt: 100_000 }));
    clock = 200_000;
    await book.loadText('OEBPS/nav.xhtml');
    expect(renew).toHaveBeenCalledTimes(1);
  });
});
