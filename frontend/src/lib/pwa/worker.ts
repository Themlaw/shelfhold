// The service worker of the PWA shell (M5 L16a, EF-LEC-012, ADR-0055). It keeps the shell of the SPA
// and its hashed assets so that the app opens with no network, and nothing else:
// - no answer of the API (`/api`), no byte of a book from the network (`/content`): a book enters an
//   offline copy only through L16b (`lib/offline`), by profile, with the headers it was served with;
// - never the shell in place of the reader (`/lire` is its own page under its own policy: the worker
//   does not answer there, online or not, and the server writes the header);
// - nothing from another origin; no eval, no dynamic import, no inline script.

import { COPY_PATH, type OfflineEnv } from '#lib/offline/env.ts';
import { isReaderAsset, isReaderPage, readerAsset, readerPage, warmReader } from '#lib/offline/reader-cache.ts';
import { serveCopyEntry } from '#lib/offline/store.ts';

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
  addEventListener: (type: 'install' | 'activate' | 'fetch' | 'message', handler: (event: never) => void) => void;
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

/** Whether the worker leaves the path to the network: the first segment of the decoded path is a
 * reserved one, or the path is odd. A path is odd when its decoded form holds a `.` or `..` segment,
 * an encoded slash (`%2F`) or a double slash: the server serves no page for it (404), so the worker
 * never answers it either (no fallback to the shell, and the same for every prefix, `/lire`,
 * `/content`, `/api`). One rule on both sides (`odd_path` of `spa.py`): the two never read one line
 * as two paths. A path that does not decode is reserved. */
export function isOdd(pathname: string): boolean {
  if (/%2f/i.test(pathname)) return true;
  let decoded: string;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return true;
  }
  return decoded.includes('//') || decoded.split('/').some((segment) => segment === '.' || segment === '..');
}

export function isReserved(pathname: string): boolean {
  if (isOdd(pathname)) return true;
  const segments = decodeURIComponent(pathname).split('/');
  return RESERVED.has((segments.find((segment) => segment !== '') ?? '').toLowerCase());
}

/** A name for a build: the cache of the shell changes with the files it holds. */
export function versionOf(manifest: PrecacheEntry[]): string {
  let hash = 0x811c9dc5;
  for (const char of JSON.stringify(manifest)) hash = Math.imul(hash ^ char.charCodeAt(0), 0x01000193) >>> 0;
  return hash.toString(36);
}

export function installPwa(scope: PwaScope, manifest: PrecacheEntry[], version: string, offline?: OfflineEnv): void {
  const name = `${SHELL_CACHE_PREFIX}${version}`;
  const origin = scope.location.origin;
  const shell = `${origin}/`;

  const kept = manifest
    .map((entry) => new URL(entry.url === 'index.html' ? '/' : entry.url, shell))
    .filter((url) => url.origin === origin && !isReserved(url.pathname))
    .map((url) => url.href);

  async function precache(): Promise<void> {
    if (!kept.includes(shell)) throw new Error('the manifest holds no shell: the worker is not installed');
    const cache = await scope.caches.open(name);
    try {
      await Promise.all(
        kept.map(async (href) => {
          const response = await scope.fetch(new Request(href, { cache: 'reload' }));
          // a redirect (a proxy that asks to sign in, say) is not the page: it would be kept as the shell
          if (response.status !== 200 || response.redirected) {
            throw new Error(`the shell cannot be kept: ${href} answered ${response.status}${response.redirected ? ' after a redirect' : ''}`);
          }
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
    if (url.origin !== origin) return;
    // The one answer of the worker under `/content`: the copy a profile chose to keep (L16b), under a
    // name no server ticket can have. Only the active profile's, headers as served, never the network.
    if (offline && COPY_PATH.test(url.pathname) && !isOdd(url.pathname)) {
      event.respondWith(serveCopyEntry(offline, url));
      return;
    }
    // The reader opens with no network: its page and its assets are kept as served, with their headers
    // (never the shell of the SPA in their place). Only with an offline store, i.e. in the SPA's worker.
    if (offline && !isOdd(url.pathname)) {
      // a navigation, and the reader's own read of its page (the guard of its policy asks for the header
      // the page came with: a `fetch` of the same address, which must find the same kept page)
      if ((request.mode === 'navigate' || request.mode === 'same-origin' || request.mode === 'cors') && isReaderPage(url.pathname)) {
        event.respondWith(readerPage(scope, offline, request));
        return;
      }
      if (isReaderAsset(url.pathname)) {
        event.respondWith(readerAsset(scope, request));
        return;
      }
    }
    if (isReserved(url.pathname)) return; // the browser asks the network
    if (request.mode === 'navigate') event.respondWith(page(request));
    else if (ASSET.test(url.pathname)) event.respondWith(asset(request));
  }) as (event: never) => void);
  if (offline) {
    // the page that has just kept a copy asks for the reader to be kept as well
    scope.addEventListener('message', ((event: Waitable & { data?: { type?: unknown } }) => {
      if (event.data?.type === 'shelfhold-warm-reader') event.waitUntil(warmReader(scope, offline).catch(() => 0));
    }) as (event: never) => void);
  }
}
