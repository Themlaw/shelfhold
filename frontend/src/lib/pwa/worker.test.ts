// @vitest-environment node
// The service worker of the PWA shell (M5 L16a, EF-LEC-012; ADR-0055): the shell of the SPA and its
// hashed assets are kept, nothing else. No answer of the API, no byte of a book (`/content/*`), and
// never the shell in place of the reader (`/lire`).
import { describe, expect, it, vi } from 'vitest';
import { installPwa, isReserved, SHELL_CACHE_PREFIX, type PrecacheEntry, type PwaScope } from './worker.ts';

const ORIGIN = 'https://shelfhold.example';
const SHELL = '<!doctype html><title>shell</title>';
const MANIFEST: PrecacheEntry[] = [
  { url: 'index.html', revision: 'r1' },
  { url: '_app/immutable/entry/app.AAAA.js', revision: null },
  { url: '_app/immutable/assets/0.BBBB.css', revision: null },
  { url: 'manifest.webmanifest', revision: 'r2' }
];

class FakeCache {
  entries = new Map<string, Response>();
  async put(request: Request | string, response: Response) {
    this.entries.set(typeof request === 'string' ? request : request.url, response.clone());
  }
  async match(request: Request | string) {
    return this.entries.get(typeof request === 'string' ? request : request.url)?.clone();
  }
  async keys() {
    return [...this.entries.keys()].map((url) => new Request(url));
  }
}

class FakeStorage {
  stores = new Map<string, FakeCache>();
  async open(name: string) {
    if (!this.stores.has(name)) this.stores.set(name, new FakeCache());
    return this.stores.get(name)!;
  }
  async keys() {
    return [...this.stores.keys()];
  }
  async delete(name: string) {
    return this.stores.delete(name);
  }
  async match(request: Request | string) {
    for (const store of this.stores.values()) {
      const found = await store.match(request);
      if (found) return found;
    }
    return undefined;
  }
}

function bench(opts: { offline?: boolean; manifest?: PrecacheEntry[]; version?: string } = {}) {
  const handlers: Record<string, (event: Record<string, unknown>) => void> = {};
  const network = { offline: opts.offline ?? false, asked: [] as string[] };
  const caches = new FakeStorage();
  const scope = {
    location: { origin: ORIGIN },
    caches,
    addEventListener: (type: string, handler: (event: Record<string, unknown>) => void) => void (handlers[type] = handler),
    skipWaiting: vi.fn(async () => undefined),
    clients: { claim: vi.fn(async () => undefined) },
    fetch: vi.fn(async (input: Request | string) => {
      const url = typeof input === 'string' ? input : input.url;
      network.asked.push(url);
      if (network.offline) throw new TypeError('Failed to fetch');
      return new Response(url.endsWith('.js') ? 'export {};' : SHELL, { status: 200 });
    })
  };
  installPwa(scope as unknown as PwaScope, opts.manifest ?? MANIFEST, opts.version ?? 'v1');
  const fire = async (type: string, extra: Record<string, unknown> = {}) => {
    const waits: Promise<unknown>[] = [];
    const event: Record<string, unknown> & { answer?: Promise<Response> } = {
      ...extra,
      waitUntil: (p: Promise<unknown>) => void waits.push(p),
      respondWith: (p: Promise<Response>) => void (event.answer = p)
    };
    handlers[type]?.(event);
    await Promise.all(waits);
    return event;
  };
  const ask = (url: string, init: { method?: string; mode?: RequestMode } = {}) =>
    fire('fetch', { request: { url: new URL(url).href, method: init.method ?? 'GET', mode: init.mode ?? 'navigate' } });
  return { scope, caches, network, fire, ask };
}

const text = async (event: { answer?: Promise<Response> }) => (await event.answer)!.text();

describe('the shell is kept at install', () => {
  // req: EF-LEC-012
  it('test_pwa_offline_cache: the shell and its hashed assets are kept, each from the origin of the worker', async () => {
    const b = bench();
    await b.fire('install');
    const [name] = await b.caches.keys();
    expect(name).toBe(`${SHELL_CACHE_PREFIX}v1`);
    const kept = [...(await b.caches.open(name!)).entries.keys()].sort();
    expect(kept).toEqual(
      [`${ORIGIN}/`, `${ORIGIN}/_app/immutable/assets/0.BBBB.css`, `${ORIGIN}/_app/immutable/entry/app.AAAA.js`, `${ORIGIN}/manifest.webmanifest`].sort()
    );
    expect(b.scope.skipWaiting).toHaveBeenCalled();
  });

  // req: EF-LEC-012, ENF-SEC-004
  it('refuses to keep a reserved address, whatever the manifest says', async () => {
    const b = bench({
      manifest: [
        ...MANIFEST,
        { url: 'content/T/x.png', revision: null },
        { url: 'api/v1/me', revision: null },
        { url: 'lire/U1', revision: null },
        { url: 'reader-entry/reader.html', revision: null },
        { url: 'https://cdn.example/lib.js', revision: null }
      ]
    });
    await b.fire('install');
    const kept = [...(await b.caches.open(`${SHELL_CACHE_PREFIX}v1`)).entries.keys()];
    expect(kept.filter((u) => /content|api|lire|reader-entry|cdn\.example/.test(u))).toEqual([]);
    expect(b.network.asked.filter((u) => /content|api|lire|reader-entry|cdn\.example/.test(u))).toEqual([]);
  });

  // req: EF-LEC-012
  it('fails the install when a file of the shell cannot be had: no half shell', async () => {
    const b = bench({ offline: true });
    await expect(b.fire('install')).rejects.toBeInstanceOf(Error);
  });

  // req: EF-LEC-012
  it('fails the install when the manifest holds no shell: a worker with no page is not installed', async () => {
    const b = bench({ manifest: MANIFEST.filter((e) => e.url !== 'index.html') });
    await expect(b.fire('install')).rejects.toThrow(/shell/);
    expect(await b.caches.keys()).toEqual([]);
  });

  // req: EF-LEC-012, ENF-SEC-004
  it('refuses a file the network answered by a redirect or a status other than 200', async () => {
    for (const odd of [{ redirected: true, status: 200 }, { redirected: false, status: 206 }]) {
      const b = bench();
      b.scope.fetch.mockImplementation(async () => Object.assign(new Response('x', { status: odd.status === 206 ? 206 : 200 }), odd.redirected ? { redirected: true } : {}));
      await expect(b.fire('install')).rejects.toBeInstanceOf(Error);
      expect(await b.caches.keys()).toEqual([]); // no half shell
    }
  });

  // req: EF-LEC-012
  it('drops the shell of an older version at activation and nothing else', async () => {
    const b = bench();
    await b.caches.open(`${SHELL_CACHE_PREFIX}v0`);
    await b.caches.open('shelfhold-offline-profile-A'); // the copies of L16b are not the business of the shell
    await b.fire('install');
    await b.fire('activate');
    expect(await b.caches.keys()).toEqual(['shelfhold-offline-profile-A', `${SHELL_CACHE_PREFIX}v1`]);
    expect(b.scope.clients.claim).toHaveBeenCalled();
  });
});

describe('what the worker answers', () => {
  // req: EF-LEC-012
  it('test_pwa_offline_cache: a page of the SPA opens without a network, from the kept shell', async () => {
    const b = bench();
    await b.fire('install');
    b.network.offline = true;
    for (const path of ['/', '/bibliotheque', '/serie/abc', '/a-classer?x=1']) {
      expect(await text(await b.ask(`${ORIGIN}${path}`))).toBe(SHELL);
    }
  });

  // req: EF-LEC-012
  it('serves the page of the network when there is one (the shell is not served stale)', async () => {
    const b = bench();
    await b.fire('install');
    const event = await b.ask(`${ORIGIN}/bibliotheque`);
    expect(b.network.asked).toContain(`${ORIGIN}/bibliotheque`);
    expect(await text(event)).toBe(SHELL);
  });

  // req: EF-LEC-012
  it('serves a hashed asset from the kept copy, with no network', async () => {
    const b = bench();
    await b.fire('install');
    b.network.offline = true;
    const event = await b.ask(`${ORIGIN}/_app/immutable/entry/app.AAAA.js`, { mode: 'cors' });
    expect(await text(event)).toBe('export {};');
  });

  // req: EF-LEC-012, ENF-SEC-004
  it.each(['/lire', '/lire/', '/lire/U1', '/lire/U1/page/3?x=1', '/%6cire/U1', '/%4Cire', '/lire%2FU1', '/a/../lire/U1', '/./lire', '//lire', '//lire/U1', '/lire%2F..', '/lire%2F..%2Fx', '/lire%2F%2e%2e'])(
    'test_offline_reader_entry_keeps_reader_csp: %s never gets the shell of the SPA, online or not',
    async (path) => {
      const b = bench();
      await b.fire('install');
      b.network.offline = true;
      const event = await b.ask(`${ORIGIN}${path}`);
      expect(event.answer).toBeUndefined(); // the browser asks the network, and the server writes its own policy
      expect(b.network.asked.filter((u) => u.includes('lire'))).toEqual([]);
    }
  );

  // req: EF-LEC-012, ENF-SEC-004
  it.each(['/content/TICKET/OEBPS/c1.xhtml', '/content/TICKET/p1.jpg', '/api/v1/me', '/api/reader/unit/U1/publication', '/healthz', '/openapi.json', '/reader-entry/reader.html', '/reader-entry/assets/reader.js'])(
    'never answers, never keeps %s',
    async (path) => {
      const b = bench();
      await b.fire('install');
      for (const mode of ['navigate', 'cors', 'same-origin'] as const) {
        const event = await b.ask(`${ORIGIN}${path}`, { mode });
        expect(event.answer).toBeUndefined();
      }
      const kept = [...(await b.caches.open(`${SHELL_CACHE_PREFIX}v1`)).entries.keys()];
      expect(kept.some((u) => u.endsWith(path))).toBe(false);
    }
  );

  // req: EF-LEC-012
  it('keeps no answer of the API by default, however many it has seen', async () => {
    const b = bench();
    await b.fire('install');
    const before = [...(await b.caches.open(`${SHELL_CACHE_PREFIX}v1`)).entries.keys()].length;
    await b.ask(`${ORIGIN}/api/v1/me`, { mode: 'cors' });
    await b.ask(`${ORIGIN}/api/v1/library/works?limit=10`, { mode: 'cors' });
    await b.ask(`${ORIGIN}/bibliotheque`);
    expect([...(await b.caches.open(`${SHELL_CACHE_PREFIX}v1`)).entries.keys()].length).toBe(before);
    expect(b.network.asked.some((u) => u.includes('/api/'))).toBe(false);
  });

  // req: EF-LEC-012
  it('leaves alone what is not a plain read of its own origin', async () => {
    const b = bench();
    await b.fire('install');
    expect((await b.ask(`${ORIGIN}/bibliotheque`, { method: 'POST' })).answer).toBeUndefined();
    expect((await b.ask('https://elsewhere.example/', { mode: 'cors' })).answer).toBeUndefined();
    expect((await b.ask(`${ORIGIN}/some/other/file.png`, { mode: 'no-cors' })).answer).toBeUndefined();
  });

  // req: EF-LEC-012
  it('rejects a navigation with no shell kept and no network, rather than invent a page', async () => {
    const b = bench({ offline: true });
    const event = await b.ask(`${ORIGIN}/bibliotheque`);
    await expect(event.answer).rejects.toBeInstanceOf(TypeError);
  });
});

describe('isReserved', () => {
  // req: EF-LEC-012, ENF-SEC-004
  it('one rule with the server: an odd path (dot segment, encoded slash, double slash) has no answer, for every prefix', () => {
    for (const path of ['/lire%2F..', '/content%2F..%2Fx', '/api%2F..', '/bibliotheque%2Fx', '/bibliotheque/%2e%2e/x', '/a//b', '//x', '/_app/immutable/%2e%2e/x.js']) {
      expect(isReserved(path), path).toBe(true);
    }
    for (const path of ['/', '/bibliotheque/', '/serie/abc', '/_app/immutable/entry/app.js']) {
      expect(isReserved(path), path).toBe(false);
    }
  });

  // req: EF-LEC-012, ENF-SEC-004
  it('is decided on the decoded path, and a path that does not decode is reserved', () => {
    expect(isReserved('/lire/x')).toBe(true);
    expect(isReserved('/%6cire')).toBe(true);
    // dot segments that survive until the path is decoded (the URL parser has already removed the others)
    expect(isReserved('/x%2f..%2flire')).toBe(true);
    expect(isReserved('/_app/..%2flire/U1')).toBe(true);
    expect(isReserved('/a%2F%2e%2E%2Freader-entry/reader.html')).toBe(true);
    expect(isReserved('//lire')).toBe(true);
    expect(isReserved('/%256cire')).toBe(false); // decoded once, as the server does: `%6cire` is a screen of the SPA
    expect(isReserved('/%E0%A4%A')).toBe(true);
    expect(isReserved('/bibliotheque')).toBe(false);
    expect(isReserved('/lirex')).toBe(false); // a screen of the SPA that merely starts with the same letters
    expect(isReserved('/')).toBe(false);
  });
});
