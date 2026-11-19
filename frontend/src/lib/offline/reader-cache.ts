// The reader opens with no network (M5 L16b, EF-LEC-012; ADR-0055 N1): its page and its own
// assets are kept as they were served, headers included (the policy of the reader is a header: a
// page answered without it would be another page). The page is the same for every book, holds no
// data, and is answered only on a browser where a profile is signed in. Never the shell of the SPA
// in its place: a miss is a network error, as before.
import { ACTIVE_KV, type OfflineEnv } from './env.ts';

export const READER_CACHE = 'shelfhold-reader';
const PAGE_KEY = '/lire';
const ASSETS = '/reader-entry/assets/';
const WARM_PATH = '/lire/content/warm';
const MAX_ASSETS = 200;

export const isReaderPage = (pathname: string): boolean => pathname === '/lire' || pathname.startsWith('/lire/');
export const isReaderAsset = (pathname: string): boolean => pathname.startsWith(ASSETS) && !pathname.slice(ASSETS.length).includes('/') && pathname.length > ASSETS.length;

export interface ReaderScope {
  location: { origin: string };
  caches: CacheStorage;
  fetch: (input: Request | string) => Promise<Response>;
}

async function signedIn(env: OfflineEnv): Promise<boolean> {
  const value = await env.openKv(ACTIVE_KV).get('profile');
  return typeof value === 'string' && value !== '';
}

const keptPath = (request: Request) => new URL(request.url).pathname;

export async function readerPage(scope: ReaderScope, env: OfflineEnv, request: Request): Promise<Response> {
  let answer: Response;
  try {
    answer = await scope.fetch(request);
  } catch (error) {
    if (!(await signedIn(env))) throw error;
    const found = await (await scope.caches.open(READER_CACHE)).match(PAGE_KEY);
    if (found) return found;
    throw error;
  }
  if (answer.status === 200 && !answer.redirected && answer.headers.get('content-security-policy')) {
    const cache = await scope.caches.open(READER_CACHE);
    const before = await cache.match(PAGE_KEY);
    const text = await answer.clone().text();
    if (before && (await before.text()) !== text) {
      // another build of the reader: the assets of the old one are of no use to the new page
      for (const key of await cache.keys()) await cache.delete(key);
    }
    await cache.put(PAGE_KEY, answer.clone());
  }
  return answer;
}

export async function readerAsset(scope: ReaderScope, request: Request): Promise<Response> {
  const cache = await scope.caches.open(READER_CACHE);
  const path = keptPath(request);
  const found = await cache.match(path);
  if (found) return found;
  const answer = await scope.fetch(request);
  if (answer.status === 200 && !answer.redirected) await cache.put(path, answer.clone());
  return answer;
}

const REFERENCE = /(?:["'`(]|url\()\s*((?:\.{1,2}\/|\/reader-entry\/assets\/)?[\w.@~-]+(?:\/[\w.@~-]+)*\.(?:js|css|wasm|woff2?|ttf|png|svg))/g;

/** Keeps the page of the reader and every asset it names, the ones named by those too (the chunks it
 * loads when a book opens), so that a first reading with no network works. Bounded; only what is
 * under `/reader-entry/assets/`. */
export async function warmReader(scope: ReaderScope, env: OfflineEnv): Promise<number> {
  if (!(await signedIn(env))) return 0;
  const origin = scope.location.origin;
  const page = await readerPage(scope, env, new Request(`${origin}${WARM_PATH}`, { cache: 'reload' }));
  if (page.status !== 200) return 0;
  const pending: string[] = [];
  const seen = new Set<string>();
  const enqueue = (text: string, base: string) => {
    for (const match of text.matchAll(REFERENCE)) {
      let url: URL;
      try {
        url = new URL(match[1]!, base);
      } catch {
        continue;
      }
      if (url.origin === origin && isReaderAsset(url.pathname) && !seen.has(url.pathname) && seen.size < MAX_ASSETS) {
        seen.add(url.pathname);
        pending.push(url.pathname);
      }
    }
  };
  enqueue(await page.clone().text(), `${origin}/`);
  for (let path = pending.shift(); path !== undefined; path = pending.shift()) {
    const asset = await readerAsset(scope, new Request(`${origin}${path}`));
    if (asset.status !== 200) continue;
    if (/\.(?:js|css)$/.test(path)) enqueue(await asset.clone().text(), `${origin}${path}`);
  }
  return seen.size;
}
