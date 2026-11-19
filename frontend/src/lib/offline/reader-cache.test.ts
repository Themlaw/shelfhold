// @vitest-environment node
// The reader opens with no network (M5 L16b, EF-LEC-012, ADR-0055 N1): its page and its assets are
// kept as served, headers included, and answered only where a profile is signed in.
import { describe, expect, it, vi } from 'vitest';
import { installPwa, type PwaScope } from '#lib/pwa/worker.ts';
import { isReaderAsset, isReaderPage, readerAsset, readerPage, warmReader, type ReaderScope } from './reader-cache.ts';
import { createOfflineStore } from './store.ts';
import { memoryEnv } from './testing.ts';

const ORIGIN = 'https://shelfhold.example';
const CSP = "default-src 'none'; script-src 'strict-dynamic' 'sha256-AAAA'";
const html = (n = 1) =>
  `<!doctype html><link rel="stylesheet" href="/reader-entry/assets/reader.${n}.css"><script>boot("/reader-entry/assets/reader.${n}.js")</script>`;

function bench(page = html()) {
  const env = memoryEnv();
  const net = { offline: false, page, asked: [] as string[], files: new Map<string, string>() };
  const scope: ReaderScope = {
    location: { origin: ORIGIN },
    caches: env.caches,
    fetch: vi.fn(async (input: Request | string) => {
      const url = new URL(typeof input === 'string' ? input : input.url);
      net.asked.push(url.pathname);
      if (net.offline) throw new TypeError('Failed to fetch');
      if (url.pathname.startsWith('/lire')) return new Response(net.page, { headers: { 'content-security-policy': CSP, 'content-type': 'text/html' } });
      const file = net.files.get(url.pathname);
      return file === undefined ? new Response('nope', { status: 404 }) : new Response(file);
    })
  };
  return { env, net, scope, store: createOfflineStore(env) };
}
const nav = (path = '/lire/unit/U1') => new Request(`${ORIGIN}${path}`);

describe('the page of the reader', () => {
  // req: EF-LEC-012
  it('test_offline_reader_entry_keeps_reader_csp: answered with no network, with the header it was served with', async () => {
    const b = bench();
    await b.store.activate('P1');
    expect((await readerPage(b.scope, b.env, nav())).status).toBe(200); // online: kept on the way
    b.net.offline = true;
    const answer = await readerPage(b.scope, b.env, nav('/lire/content/OTHER'));
    expect(answer.headers.get('content-security-policy')).toBe(CSP);
    expect(await answer.text()).toBe(html());
  });

  // req: EF-LEC-012, ENF-SEC-010
  it('nobody signed in on this browser: a network error, not a page', async () => {
    const b = bench();
    await readerPage(b.scope, b.env, nav());
    b.net.offline = true;
    await expect(readerPage(b.scope, b.env, nav())).rejects.toThrow(TypeError);
    await b.store.activate('P1');
    expect((await readerPage(b.scope, b.env, nav())).status).toBe(200);
    await b.store.wipeAll(); // signed out: the page is no longer answered
    await expect(readerPage(b.scope, b.env, nav())).rejects.toThrow(TypeError);
  });

  // req: EF-LEC-012
  it('a page with no policy header is never kept (it would be another page)', async () => {
    const b = bench();
    await b.store.activate('P1');
    (b.scope.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(new Response('<html>shell</html>'));
    await readerPage(b.scope, b.env, nav());
    b.net.offline = true;
    await expect(readerPage(b.scope, b.env, nav())).rejects.toThrow(TypeError);
  });

  // req: EF-LEC-012
  it('another build of the page drops the assets of the old one', async () => {
    const b = bench();
    await b.store.activate('P1');
    b.net.files.set('/reader-entry/assets/reader.1.js', 'old');
    await readerPage(b.scope, b.env, nav());
    await readerAsset(b.scope, new Request(`${ORIGIN}/reader-entry/assets/reader.1.js`));
    b.net.page = html(2);
    await readerPage(b.scope, b.env, nav());
    b.net.offline = true;
    await expect(readerAsset(b.scope, new Request(`${ORIGIN}/reader-entry/assets/reader.1.js`))).rejects.toThrow(TypeError);
  });
});

describe('the assets of the reader', () => {
  // req: EF-LEC-012
  it('are kept on the way and answered from the copy; a miss or a non-200 is never kept', async () => {
    const b = bench();
    b.net.files.set('/reader-entry/assets/a.js', 'A');
    const url = new Request(`${ORIGIN}/reader-entry/assets/a.js`);
    expect(await (await readerAsset(b.scope, url)).text()).toBe('A');
    b.net.offline = true;
    expect(await (await readerAsset(b.scope, url)).text()).toBe('A');
    const missing = new Request(`${ORIGIN}/reader-entry/assets/gone.js`);
    b.net.offline = false;
    expect((await readerAsset(b.scope, missing)).status).toBe(404);
    b.net.offline = true;
    await expect(readerAsset(b.scope, missing)).rejects.toThrow(TypeError);
  });

  // req: EF-LEC-012
  it('only the flat assets of the reader are assets', () => {
    expect(isReaderAsset('/reader-entry/assets/a.js')).toBe(true);
    for (const p of ['/reader-entry/assets/', '/reader-entry/assets/x/y.js', '/reader-entry/reader.html', '/_app/immutable/a.js']) expect(isReaderAsset(p)).toBe(false);
    expect(isReaderPage('/lire/unit/U')).toBe(true);
    expect(isReaderPage('/lirex')).toBe(false);
  });
});

describe('keeping the reader once, online', () => {
  // req: EF-LEC-012
  it('fetches the page, the assets it names and the ones they name, and nothing else', async () => {
    const b = bench();
    await b.store.activate('P1');
    b.net.files.set('/reader-entry/assets/reader.1.js', 'import("./chunk.js"); fetch("/api/v1/me"); "https://cdn.example/x.js"');
    b.net.files.set('/reader-entry/assets/reader.1.css', '@font-face{src:url(./f.woff2)}');
    b.net.files.set('/reader-entry/assets/chunk.js', 'export {}');
    b.net.files.set('/reader-entry/assets/f.woff2', 'font');
    expect(await warmReader(b.scope, b.env)).toBe(4);
    expect(b.net.asked.filter((p) => !p.startsWith('/reader-entry/') && !p.startsWith('/lire'))).toEqual([]);
    b.net.offline = true;
    for (const name of ['reader.1.js', 'chunk.js', 'f.woff2']) {
      expect((await readerAsset(b.scope, new Request(`${ORIGIN}/reader-entry/assets/${name}`))).status).toBe(200);
    }
  });

  // req: EF-LEC-012
  it('does nothing when nobody is signed in', async () => {
    const b = bench();
    expect(await warmReader(b.scope, b.env)).toBe(0);
    expect(b.net.asked).toEqual([]);
  });
});

describe('the worker with the offline store', () => {
  function worker() {
    const b = bench();
    const handlers: Record<string, (event: Record<string, unknown>) => void> = {};
    const scope = {
      location: { origin: ORIGIN }, caches: b.env.caches, fetch: b.scope.fetch,
      addEventListener: (t: string, h: (event: Record<string, unknown>) => void) => void (handlers[t] = h),
      skipWaiting: async () => undefined, clients: { claim: async () => undefined }
    };
    installPwa(scope as unknown as PwaScope, [{ url: 'index.html', revision: 'r' }], 'v1', b.env);
    const ask = async (path: string, mode = 'navigate', method = 'GET') => {
      const event: Record<string, unknown> & { answer?: Promise<Response> } = {
        request: new Request(`${ORIGIN}${path}`, { method, mode: mode === 'navigate' ? undefined : (mode as RequestMode) }),
        respondWith: (p: Promise<Response>) => void (event.answer = p)
      };
      Object.defineProperty(event.request, 'mode', { value: mode });
      handlers['fetch']?.(event);
      return event.answer;
    };
    return { ...b, ask, handlers };
  }

  // req: EF-LEC-012
  it('answers a navigation to /lire from the kept page, never from the shell; leaves odd paths and POST alone', async () => {
    const w = worker();
    await w.store.activate('P1');
    await (await w.ask('/lire/unit/U1'))!;
    w.net.offline = true;
    const offline = await (await w.ask('/lire/unit/U2'))!;
    expect(offline.headers.get('content-security-policy')).toBe(CSP);
    expect(await offline.text()).toBe(html());
    expect(await w.ask('/lire%2F..')).toBeUndefined();
    expect(await w.ask('//lire')).toBeUndefined();
    expect(await w.ask('/lire/unit/U1', 'navigate', 'POST')).toBeUndefined();
  });

  // req: EF-LEC-012
  it('answers the reader\'s own read of its page (the guard of its policy) from the kept page with no network', async () => {
    const w = worker();
    await w.store.activate('P1');
    await (await w.ask('/lire/unit/U1'))!;
    w.net.offline = true;
    const guard = await (await w.ask('/lire/unit/U1', 'same-origin'))!;
    expect(guard.headers.get('content-security-policy')).toBe(CSP);
    expect(await w.ask('/lire/unit/U1', 'no-cors')).toBeUndefined();
  });

  // req: EF-LEC-012
  it('keeps the reader when the page asks for it (message)', async () => {
    const w = worker();
    await w.store.activate('P1');
    w.net.files.set('/reader-entry/assets/reader.1.js', 'x');
    w.net.files.set('/reader-entry/assets/reader.1.css', 'x');
    const waits: Promise<unknown>[] = [];
    w.handlers['message']?.({ data: { type: 'shelfhold-warm-reader' }, waitUntil: (p: Promise<unknown>) => void waits.push(p) });
    w.handlers['message']?.({ data: { type: 'other' }, waitUntil: (p: Promise<unknown>) => void waits.push(p) });
    expect(waits).toHaveLength(1);
    await Promise.all(waits);
    w.net.offline = true;
    expect((await readerAsset(w.scope, new Request(`${ORIGIN}/reader-entry/assets/reader.1.js`))).status).toBe(200);
  });
});
