// The service worker of the PWA shell (M5 L16a, EF-LEC-012, ADR-0055). It keeps the shell of the SPA
// and its hashed assets so that the app opens with no network, and nothing else:
// - no answer of the API (`/api`), no byte of a book (`/content`): a book enters an offline copy
//   only through L16b, by profile, with its own headers;
// - never the shell in place of the reader (`/lire` is its own page under its own policy: the worker
//   does not answer there, online or not, and the server writes the header);
// - nothing from another origin; no eval, no dynamic import, no inline script.

export const SHELL_CACHE_PREFIX = 'shelfhold-shell-';

export interface PrecacheEntry {
  url: string;
  revision: string | null;
}

/** The part of a service worker global scope the worker uses (the rest is not reachable from here). */
export interface PwaScope {
  location: { origin: string };
  caches: CacheStorage;
  fetch: (input: Request | string) => Promise<Response>;
  skipWaiting: () => Promise<void>;
  clients: { claim: () => Promise<void> };
  addEventListener: (type: 'install' | 'activate' | 'fetch', handler: (event: never) => void) => void;
}

interface Waitable {
  waitUntil: (work: Promise<unknown>) => void;
}
interface Fetching {
  request: Request;
  respondWith: (answer: Promise<Response>) => void;
}

// First segments of the paths the worker never answers: the reader, the API, the content of a book,
// the probes, the reader's own assets (they are served with the page and its header).
const RESERVED = new Set(['lire', 'content', 'api', 'healthz', 'readyz', 'openapi.json', 'reader-entry', 'service-worker.js']);
// What is answered from the kept shell: the hashed assets of the build and the manifest.
const ASSET = /^\/(?:_app\/immutable\/|manifest\.webmanifest$|icons\/)/;

/** Whether the first segment of the path, decoded and with its dot segments resolved, is one the
 * worker leaves to the network. Decided on the decoded path, as the server reads it (`/%6cire` is
 * the reader); a path that does not decode is reserved. */
export function isReserved(pathname: string): boolean {
  let decoded: string;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return true;
  }
  const segments: string[] = [];
  for (const segment of decoded.split('/')) {
    if (segment === '' || segment === '.') continue;
    if (segment === '..') segments.pop();
    else segments.push(segment);
  }
  return RESERVED.has((segments[0] ?? '').toLowerCase());
}

/** A name for a build: the cache of the shell changes with the files it holds. */
export function versionOf(manifest: PrecacheEntry[]): string {
  let hash = 0x811c9dc5;
  for (const char of JSON.stringify(manifest)) hash = Math.imul(hash ^ char.charCodeAt(0), 0x01000193) >>> 0;
  return hash.toString(36);
}

export function installPwa(scope: PwaScope, manifest: PrecacheEntry[], version: string): void {
  const name = `${SHELL_CACHE_PREFIX}${version}`;
  const origin = scope.location.origin;
  const shell = `${origin}/`;

  const kept = manifest
    .map((entry) => new URL(entry.url === 'index.html' ? '/' : entry.url, shell))
    .filter((url) => url.origin === origin && !isReserved(url.pathname))
    .map((url) => url.href);

  async function precache(): Promise<void> {
    const cache = await scope.caches.open(name);
    try {
      await Promise.all(
        kept.map(async (href) => {
          const response = await scope.fetch(new Request(href, { cache: 'reload' }));
          if (!response.ok) throw new Error(`the shell cannot be kept: ${href} answered ${response.status}`);
          await cache.put(href, response);
        })
      );
    } catch (error) {
      await scope.caches.delete(name); // no half shell
      throw error;
    }
    await scope.skipWaiting();
  }

  async function dropOlder(): Promise<void> {
    for (const key of await scope.caches.keys()) {
      if (key.startsWith(SHELL_CACHE_PREFIX) && key !== name) await scope.caches.delete(key);
    }
    await scope.clients.claim();
  }

  async function page(request: Request): Promise<Response> {
    try {
      return await scope.fetch(request);
    } catch (error) {
      const cache = await scope.caches.open(name);
      const found = await cache.match(shell);
      if (found) return found;
      throw error;
    }
  }

  async function asset(request: Request): Promise<Response> {
    const cache = await scope.caches.open(name);
    return (await cache.match(request.url)) ?? scope.fetch(request);
  }

  scope.addEventListener('install', ((event: Waitable) => event.waitUntil(precache())) as (event: never) => void);
  scope.addEventListener('activate', ((event: Waitable) => event.waitUntil(dropOlder())) as (event: never) => void);
  scope.addEventListener('fetch', ((event: Fetching) => {
    const { request } = event;
    if (request.method !== 'GET') return;
    const url = new URL(request.url);
    if (url.origin !== origin || isReserved(url.pathname)) return; // the browser asks the network
    if (request.mode === 'navigate') event.respondWith(page(request));
    else if (ASSET.test(url.pathname)) event.respondWith(asset(request));
  }) as (event: never) => void);
}
