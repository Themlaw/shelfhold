import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Locator, PublicationView } from '../engine.ts';
import { createImageEngine, keyAction } from './images.ts';

const MIB = 1024 * 1024;

function publication(over: Partial<PublicationView> = {}, count = 10): PublicationView {
  return {
    kind: 'pages',
    scope: 'full',
    readingDirection: 'ltr',
    title: 'Une série',
    language: 'fr',
    pages: Array.from({ length: count }, (_, i) => ({
      index: i,
      href: `/content/TICKET/p${i + 1}.jpg`,
      mediaType: 'image/jpeg'
    })),
    spine: [],
    package: null,
    ...over
  };
}

/** A fetch that answers every page of the publication with `size` bytes, and counts what it served. */
function bench(size = 1024) {
  const asked: string[] = [];
  const fetcher = vi.fn<typeof fetch>(async (input) => {
    asked.push(String(input));
    return new Response(new Uint8Array(size), { status: 200, headers: { 'Content-Type': 'image/jpeg' } });
  });
  let n = 0;
  const live = new Set<string>();
  const createObjectURL = vi.fn(() => {
    const url = `blob:test/${(n += 1)}`;
    live.add(url);
    return url;
  });
  const revokeObjectURL = vi.fn((url: string) => void live.delete(url));
  return { asked, fetcher, live, createObjectURL, revokeObjectURL };
}

const flush = async () => {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
};

async function opened(pub: PublicationView, size = 1024, options: Record<string, number> = {}) {
  const b = bench(size);
  const host = document.createElement('div');
  document.body.append(host);
  const engine = createImageEngine({
    fetch: b.fetcher,
    createObjectURL: b.createObjectURL,
    revokeObjectURL: b.revokeObjectURL,
    ...options
  });
  const seen: Locator[] = [];
  engine.onLocator((l) => seen.push(l));
  await engine.open(pub, host);
  await flush();
  return { engine, host, seen, ...b };
}

afterEach(() => {
  document.body.replaceChildren();
});

describe('the image engine', () => {
  // req: EF-LEC-001
  it('reader emits a locator on page turn', async () => {
    const { engine, seen } = await opened(publication());
    engine.next();
    engine.next();
    engine.previous();
    expect(seen).toEqual([
      { v: 1, type: 'page', index: 1, count: 10 },
      { v: 1, type: 'page', index: 2, count: 10 },
      { v: 1, type: 'page', index: 1, count: 10 }
    ]);
  });

  // req: EF-LEC-001
  it('goes to a locator, and refuses one that is not of this publication', async () => {
    const { engine, seen } = await opened(publication());
    engine.goTo({ v: 1, type: 'page', index: 6, count: 10 });
    expect(seen.at(-1)).toEqual({ v: 1, type: 'page', index: 6, count: 10 });
    engine.goTo({ v: 1, type: 'page', index: 99, count: 10 });
    engine.goTo({ v: 1, type: 'page', index: -1, count: 10 });
    engine.goTo({ v: 1, type: 'page', index: 2, count: 11 }); // a place of a book of another length
    expect(seen).toHaveLength(1);
  });

  // req: EF-LEC-001
  it('reads right to left: the arrows are turned around, the page numbers are not', () => {
    expect(keyAction('ltr', 'ArrowRight')).toBe('next');
    expect(keyAction('ltr', 'ArrowLeft')).toBe('previous');
    expect(keyAction('rtl', 'ArrowLeft')).toBe('next');
    expect(keyAction('rtl', 'ArrowRight')).toBe('previous');
    expect(keyAction('rtl', 'PageDown')).toBe('next');
    expect(keyAction('ltr', ' ')).toBe('next');
    expect(keyAction('ltr', 'a')).toBeNull();
  });

  // req: EF-LEC-001
  it('puts the right-to-left page on the page element and labels it with its number', async () => {
    const { host } = await opened(publication({ readingDirection: 'rtl' }));
    expect(host.querySelector('[dir="rtl"]')).not.toBeNull();
    const img = host.querySelector('img');
    expect(img?.getAttribute('alt')).toBe('Page 1 sur 10');
  });

  // req: EF-LEC-002
  it('shows a webtoon as one column of pages with no space between them', async () => {
    const { host, engine, seen } = await opened(publication({ readingDirection: 'webtoon' }, 6));
    const column = host.querySelector('[data-reader-column]') as HTMLElement;
    expect(column).not.toBeNull();
    expect(column.querySelectorAll('img')).toHaveLength(6);
    expect(column.style.gap).toBe('0px');
    expect(column.querySelector('img')?.style.display).toBe('block');
    engine.reportVisible(3);
    expect(seen.at(-1)).toEqual({ v: 1, type: 'page', index: 3, count: 6 });
  });

  // req: EF-LEC-002
  it('does not say a page again that it already said', async () => {
    const { engine, seen } = await opened(publication({ readingDirection: 'webtoon' }, 6));
    engine.reportVisible(2);
    engine.reportVisible(2);
    expect(seen).toHaveLength(1);
  });

  // req: EF-UX-006
  it('excerpt stops at its bound', async () => {
    const { engine, seen, asked } = await opened(publication({ scope: 'excerpt' }, 3));
    const ended = vi.fn();
    engine.onEnd(ended);
    engine.next();
    engine.next();
    expect(ended).not.toHaveBeenCalled();
    engine.next(); // past the last page of the extract
    expect(ended).toHaveBeenCalledTimes(1);
    expect(seen.map((l) => (l.type === 'page' ? l.index : -1))).toEqual([1, 2]);
    expect(asked.length).toBeGreaterThan(0);
    expect(asked.every((url) => /\/p[123]\.jpg$/.test(url))).toBe(true);
    expect(asked.some((url) => /p4/.test(url))).toBe(false);
  });

  // req: EF-UX-006
  it('says "end of the extract" when the server refuses a page, not a blank page', async () => {
    const b = bench();
    b.fetcher.mockImplementation(async (input) => {
      b.asked.push(String(input));
      return String(input).endsWith('p3.jpg') ? new Response(null, { status: 404 }) : new Response(new Uint8Array(8));
    });
    const host = document.createElement('div');
    const engine = createImageEngine({
      fetch: b.fetcher,
      createObjectURL: b.createObjectURL,
      revokeObjectURL: b.revokeObjectURL
    });
    const ended = vi.fn();
    engine.onEnd(ended);
    await engine.open(publication({ scope: 'excerpt' }, 3), host);
    engine.goTo({ v: 1, type: 'page', index: 2, count: 3 });
    await flush();
    expect(ended).toHaveBeenCalledTimes(1);
    const shown = host.querySelector('img');
    expect(shown?.getAttribute('alt')).toBe('Page 3 sur 3');
    expect(shown?.hasAttribute('src')).toBe(false); // no stale page, no blank one drawn as a page
  });

  // ENF-SEC-010: every byte of the reader comes through the content route of the publication
  // req: ENF-SEC-004
  it('asks for nothing but the links the publication gave, with the session and no referrer', async () => {
    const { fetcher, asked } = await opened(publication({}, 5));
    expect(asked.length).toBeGreaterThan(0);
    expect(asked.every((url) => url.startsWith('/content/TICKET/'))).toBe(true);
    for (const call of fetcher.mock.calls) {
      const init = call[1] as RequestInit;
      expect(init.credentials).toBe('same-origin');
      expect(init.referrerPolicy).toBe('no-referrer');
    }
  });

  // req: EF-LEC-002
  it('refuses a link that does not lead into /content', async () => {
    const b = bench();
    const host = document.createElement('div');
    const engine = createImageEngine({
      fetch: b.fetcher,
      createObjectURL: b.createObjectURL,
      revokeObjectURL: b.revokeObjectURL
    });
    const pub = publication({}, 2);
    pub.pages[1] = { index: 1, href: 'https://elsewhere.example/p2.jpg', mediaType: 'image/jpeg' };
    await engine.open(pub, host);
    engine.next();
    await flush();
    expect(b.asked.some((url) => url.includes('elsewhere'))).toBe(false);
  });

  // req: EF-LEC-001
  it('preloads a bounded number of pages ahead, and no more', async () => {
    const { asked } = await opened(publication({}, 40), 1024, { preloadPages: 3 });
    expect(asked).toHaveLength(4); // the page, and three ahead
  });

  // req: EF-LEC-001
  it('keeps the bytes of the preload under its budget, and lets the far pages go', async () => {
    const { engine, live, revokeObjectURL } = await opened(publication({}, 20), MIB, {
      preloadPages: 10,
      preloadBytes: 3 * MIB
    });
    expect(engine.cachedBytes()).toBeLessThanOrEqual(3 * MIB);
    for (let i = 0; i < 12; i += 1) {
      engine.next();
      await flush();
      expect(engine.cachedBytes()).toBeLessThanOrEqual(3 * MIB);
    }
    expect(revokeObjectURL).toHaveBeenCalled();
    expect(live.size).toBeLessThanOrEqual(3);
  });

  // req: EF-LEC-001
  it('frees every page it holds when it is destroyed', async () => {
    const { engine, live, host } = await opened(publication({}, 6));
    expect(live.size).toBeGreaterThan(0);
    engine.destroy();
    expect(live.size).toBe(0);
    expect(host.childElementCount).toBe(0);
  });

  // req: ENF-SEC-004
  it('declares what it reads and no guard that it would not keep', () => {
    const engine = createImageEngine();
    expect(engine.mediaTypes).toEqual(['image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/avif']);
    expect(engine.mediaTypes).not.toContain('image/svg+xml');
    expect(engine.guards).toEqual({ noIframe: true, rasterOnly: true });
  });
  // req: ENF-SEC-004
  it('never makes a page of what the server calls an SVG or a document', async () => {
    const b = bench();
    b.fetcher.mockImplementation(async (input) => {
      b.asked.push(String(input));
      return new Response('<svg xmlns="http://www.w3.org/2000/svg"/>', { headers: { 'Content-Type': 'image/svg+xml' } });
    });
    const host = document.createElement('div');
    const engine = createImageEngine({ fetch: b.fetcher, createObjectURL: b.createObjectURL, revokeObjectURL: b.revokeObjectURL });
    await engine.open(publication({}, 2), host);
    await flush();
    expect(b.asked.length).toBeGreaterThan(0);
    expect(b.createObjectURL).not.toHaveBeenCalled();
    expect(host.querySelector('img')?.hasAttribute('src')).toBe(false);
  });

  // req: EF-LEC-001
  it('asks for new links when a ticket ran out in a long reading, and goes on', async () => {
    const b = bench();
    b.fetcher.mockImplementation(async (input) => {
      b.asked.push(String(input));
      return String(input).includes('/OLD/') ? new Response(null, { status: 404 }) : new Response(new Uint8Array(8));
    });
    const old = publication({}, 4);
    old.pages = old.pages.map((p) => ({ ...p, href: p.href.replace('TICKET', 'OLD') }));
    const fresh = vi.fn(async () => publication({}, 4));
    const host = document.createElement('div');
    const engine = createImageEngine({
      fetch: b.fetcher,
      createObjectURL: b.createObjectURL,
      revokeObjectURL: b.revokeObjectURL,
      renew: fresh
    });
    const ended = vi.fn();
    const errors = vi.fn();
    engine.onEnd(ended);
    engine.onError(errors);
    await engine.open(old, host);
    await flush();
    expect(fresh).toHaveBeenCalledTimes(1); // one renewal for all the pages that were refused together
    expect(host.querySelector('img')?.hasAttribute('src')).toBe(true);
    expect(ended).not.toHaveBeenCalled();
    expect(errors).not.toHaveBeenCalled();
  });

  // req: EF-LEC-001
  it('does not say "end of the reading" on a full book whose page the server refuses', async () => {
    const b = bench();
    b.fetcher.mockImplementation(async () => new Response(null, { status: 404 }));
    const host = document.createElement('div');
    const engine = createImageEngine({
      fetch: b.fetcher,
      createObjectURL: b.createObjectURL,
      revokeObjectURL: b.revokeObjectURL,
      renew: async () => null
    });
    const ended = vi.fn();
    const errors = vi.fn();
    engine.onEnd(ended);
    engine.onError(errors);
    await engine.open(publication({ scope: 'full' }, 3), host);
    await flush();
    expect(ended).not.toHaveBeenCalled(); // the end of an extract is a bound, not a refusal
    expect(errors).toHaveBeenCalledWith({ kind: 'missing', index: 0 });
  });

  // req: EF-LEC-001
  it('asks again later for a page that failed for a reason that may pass', async () => {
    const b = bench();
    let down = true;
    b.fetcher.mockImplementation(async (input) => {
      b.asked.push(String(input));
      return down ? new Response(null, { status: 503 }) : new Response(new Uint8Array(8));
    });
    const host = document.createElement('div');
    const engine = createImageEngine({ fetch: b.fetcher, createObjectURL: b.createObjectURL, revokeObjectURL: b.revokeObjectURL });
    const errors = vi.fn();
    engine.onError(errors);
    await engine.open(publication({}, 3), host);
    await flush();
    expect(errors).toHaveBeenCalled();
    down = false;
    engine.goTo({ v: 1, type: 'page', index: 0, count: 3 });
    await flush();
    expect(host.querySelector('img')?.hasAttribute('src')).toBe(true);
  });

  // req: EF-LEC-002
  it('asks, in a webtoon, only for the pages near the reader whatever the observer reports', async () => {
    const callbacks: ((entries: { isIntersecting: boolean; target: Element }[]) => void)[] = [];
    class FakeObserver {
      constructor(callback: (entries: { isIntersecting: boolean; target: Element }[]) => void) {
        callbacks.push(callback);
      }
      observe() {}
      disconnect() {}
    }
    vi.stubGlobal('IntersectionObserver', FakeObserver);
    try {
      const { host, asked } = await opened(publication({ readingDirection: 'webtoon' }, 300), 1024, { preloadPages: 2 });
      const before = asked.length;
      const everyPage = [...host.querySelectorAll('img')].map((target) => ({ isIntersecting: true, target }));
      callbacks[0]?.(everyPage); // the margin of the observer reaches all the pages at once
      await flush();
      expect(asked.length - before).toBeLessThanOrEqual(1);
      expect(asked.length).toBeLessThanOrEqual(4);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
