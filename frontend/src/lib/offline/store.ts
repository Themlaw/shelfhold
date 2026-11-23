// The offline copies of the PWA (M5 L16b, EF-LEC-012, EF-LEC-007; ADR-0055 « Crédential »).
//
// A copy belongs to **one profile**: its bytes are in a cache named after the profile, the rest (the
// manifest of the book, the positions waiting, the `?since=` token) in a store named after it.
// One profile is *active* on a browser (the one who signed in); activating another wipes the
// previous one, so a copy is never readable by someone else on the same device. Sign-out wipes
// everything. Nothing here reads a cookie or a secret: an offline copy is as far as the browser can
// go, and R66 says what it cannot do (a copy cannot be revoked from the server while offline).
import type { PositionIn, PublicationOut } from '#lib/api/types.gen.ts';
import { ACTIVE_KV, COPY_CACHE_PREFIX, COPY_PATH, copyCacheName, profileKvName, profileOfKvName, type OfflineEnv } from './env.ts';

export type CopyCarrier = 'content' | 'export_item';

export interface CopyMeta {
  carrierType: CopyCarrier;
  carrierId: string;
  workId: string;
  title: string;
  keptAt: string;
  /** The Unit it was asked for by, when it was (the reader opens `unit/<id>`). */
  via?: string;
  publication: PublicationOut;
}

export interface QueuedPosition {
  key: string;
  body: PositionIn;
}

export interface CopyEntry {
  url: string;
  response: Response;
}

export interface OfflineStore {
  activate(profileId: string): Promise<void>;
  activeProfile(): Promise<string | null>;
  keep(profileId: string, meta: Omit<CopyMeta, 'keptAt'>, entries: Iterable<CopyEntry> | AsyncIterable<CopyEntry>): Promise<void>;
  copies(profileId: string): Promise<CopyMeta[]>;
  find(profileId: string, carrierType: string, carrierId: string): Promise<CopyMeta | undefined>;
  evictWorks(profileId: string, workIds: readonly string[]): Promise<string[]>;
  evictCarrier(profileId: string, carrierType: string, carrierId: string): Promise<void>;
  wipe(profileId: string): Promise<void>;
  wipeAll(): Promise<void>;
  /** Ties the profile on this browser to the session that is signed in (a hash of its anti-forgery token). */
  bindSession(token: string): Promise<void>;
  /** Whether this session is the one the profile was bound to; null when none was recorded. */
  sessionMatches(token: string): Promise<boolean | null>;
  queuePosition(profileId: string, body: PositionIn): Promise<void>;
  queued(profileId: string): Promise<QueuedPosition[]>;
  dropPosition(profileId: string, key: string): Promise<void>;
  since(profileId: string): Promise<string | null>;
  setSince(profileId: string, token: string): Promise<void>;
}

const COPY = 'copy:';
const POSITION = 'pos:';
const carrierKey = (type: string, id: string) => `${type}:${id}`;
const prefixOf = (type: string, id: string) => `/content/offline.${type}.${id}/`;
const stamp = (value: string) => (Number.isFinite(Date.parse(value)) ? Date.parse(value) : 0);

async function digest(token: string): Promise<string> {
  const bytes = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(`shelfhold/offline-session/v1/${token}`));
  return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export function createOfflineStore(env: OfflineEnv): OfflineStore {
  // Every change goes one at a time: in this page by a chain, across tabs by a Web Lock. A read then
  // write of the queue, a copy kept while another profile signs in, a wipe in the middle of a sync:
  // none interleaves. (A leaf is `exclusive`; what it calls is not, or it would wait for itself.)
  let chain: Promise<unknown> = Promise.resolve();
  function exclusive<T>(work: () => Promise<T>): Promise<T> {
    const run = chain.then(() => (env.lock ? env.lock('shelfhold-offline', work) : work()));
    chain = run.catch(() => undefined);
    return run;
  }
  const active = env.openKv(ACTIVE_KV);
  const own = (profileId: string) => env.openKv(profileKvName(profileId));

  async function activeProfile(): Promise<string | null> {
    const value = await active.get('profile');
    return typeof value === 'string' && value !== '' ? value : null;
  }

  async function known(): Promise<string[]> {
    const value = await active.get('known');
    return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
  }

  async function remember(profileId: string): Promise<void> {
    const list = await known();
    if (!list.includes(profileId)) await active.set('known', [...list, profileId]);
  }

  async function requireActive(profileId: string): Promise<void> {
    if ((await activeProfile()) !== profileId) throw new Error('not the active profile');
  }

  async function dropBytes(profileId: string, type: string, id: string): Promise<void> {
    if (!(await env.caches.has(copyCacheName(profileId)))) return;
    const cache = await env.caches.open(copyCacheName(profileId));
    const prefix = prefixOf(type, id);
    for (const request of await cache.keys()) {
      if (new URL(request.url, 'https://offline.invalid').pathname.startsWith(prefix)) await cache.delete(request);
    }
    if ((await cache.keys()).length === 0) await env.caches.delete(copyCacheName(profileId)); // no empty cache left
  }

  async function wipeNow(profileId: string): Promise<void> {
    await env.caches.delete(copyCacheName(profileId));
    await env.deleteKv(profileKvName(profileId));
    await active.set('known', (await known()).filter((id) => id !== profileId));
    if ((await activeProfile()) === profileId) {
      await active.delete('profile');
      await active.delete('session');
    }
  }

  async function activateNow(profileId: string): Promise<void> {
    // one profile at a time on a browser: the others' copies go, they are not hidden. Their stores
    // are found by name as well: a database of a profile no tab remembers — a crash between the
    // wipe of a profile and the write of `known`, a race of two tabs, an older version — is
    // deleted at open, not only the ones `known` lists.
    const others = new Set((await known()).filter((id) => id !== profileId));
    for (const name of await env.caches.keys()) {
      if (name.startsWith(COPY_CACHE_PREFIX) && name !== copyCacheName(profileId)) others.add(name.slice(COPY_CACHE_PREFIX.length));
    }
    for (const name of (await env.listKv?.()) ?? []) {
      const id = profileOfKvName(name);
      if (id !== null && id !== profileId) others.add(id);
    }
    for (const id of others) await wipeNow(id);
    await active.set('profile', profileId);
    await remember(profileId);
  }

  /** The download runs **outside** the lock (a long or stuck download must not hold up a sign-out or
   * another change); only the check that the profile is still the one on this browser, and the write
   * of the manifest, are made under it. A copy whose profile left meanwhile is dropped, never written. */
  async function keepCopyOf(profileId: string, meta: Omit<CopyMeta, 'keptAt'>, entries: Iterable<CopyEntry> | AsyncIterable<CopyEntry>): Promise<void> {
    await requireActive(profileId);
    const cache = await env.caches.open(copyCacheName(profileId));
    const drop = () => exclusive(() => dropBytes(profileId, meta.carrierType, meta.carrierId));
    try {
      for await (const entry of entries) await cache.put(entry.url, entry.response);
    } catch (error) {
      await drop();
      throw error;
    }
    await exclusive(async () => {
      if ((await activeProfile()) !== profileId) {
        await dropBytes(profileId, meta.carrierType, meta.carrierId);
        await env.caches.delete(copyCacheName(profileId)); // the profile is gone: nothing of it stays
        throw new Error('not the active profile');
      }
      await own(profileId).set(COPY + carrierKey(meta.carrierType, meta.carrierId), { ...meta, keptAt: new Date(env.now()).toISOString() });
    });
  }

  async function copiesOf(profileId: string): Promise<CopyMeta[]> {
    const rows = await own(profileId).entries();
    return rows.filter(([key]) => key.startsWith(COPY)).map(([, value]) => value as CopyMeta);
  }

  async function evictCarrierNow(profileId: string, type: string, id: string): Promise<void> {
    await dropBytes(profileId, type, id);
    await own(profileId).delete(COPY + carrierKey(type, id));
    // the positions read offline stay queued: removing a copy is not a reason to lose a reading
  }

  async function evictWorksNow(profileId: string, workIds: readonly string[]): Promise<string[]> {
    const wanted = new Set(workIds);
    const gone: string[] = [];
    for (const copy of await copiesOf(profileId)) {
      if (!wanted.has(copy.workId)) continue;
      await evictCarrierNow(profileId, copy.carrierType, copy.carrierId);
      if (!gone.includes(copy.workId)) gone.push(copy.workId);
    }
    return gone;
  }

  async function wipeAllNow(): Promise<void> {
    await active.delete('profile'); // first: if the rest fails, nothing is served any more
    const ids = new Set(await known());
    for (const name of await env.caches.keys()) {
      if (name.startsWith(COPY_CACHE_PREFIX)) ids.add(name.slice(COPY_CACHE_PREFIX.length));
    }
    for (const name of (await env.listKv?.()) ?? []) {
      const id = profileOfKvName(name);
      if (id !== null) ids.add(id);
    }
    for (const id of ids) await wipeNow(id);
    await env.deleteKv(ACTIVE_KV);
  }

  async function queueNow(profileId: string, body: PositionIn): Promise<void> {
    await requireActive(profileId);
    const key = POSITION + carrierKey(body.carrier_type, body.carrier_id);
    const waiting = (await own(profileId).get(key)) as QueuedPosition | undefined;
    if (waiting && stamp(waiting.body.client_ts) > stamp(body.client_ts)) return; // the later reading wins
    await own(profileId).set(key, { key, body } satisfies QueuedPosition);
  }

  return {
    activeProfile,
    activate: (profileId) => exclusive(() => activateNow(profileId)),
    keep: keepCopyOf,
    copies: copiesOf,
    async find(profileId, carrierType, carrierId) {
      const direct = (await own(profileId).get(COPY + carrierKey(carrierType, carrierId))) as CopyMeta | undefined;
      if (direct || carrierType !== 'unit') return direct;
      return (await copiesOf(profileId)).find((copy) => copy.via === carrierId);
    },
    evictCarrier: (profileId, type, id) => exclusive(() => evictCarrierNow(profileId, type, id)),
    evictWorks: (profileId, workIds) => exclusive(() => evictWorksNow(profileId, workIds)),
    wipe: (profileId) => exclusive(() => wipeNow(profileId)),
    wipeAll: () => exclusive(wipeAllNow),
    queuePosition: (profileId, body) => exclusive(() => queueNow(profileId, body)),
    async queued(profileId) {
      const rows = await own(profileId).entries();
      return rows
        .filter(([key]) => key.startsWith(POSITION))
        .map(([, value]) => value as QueuedPosition)
        .sort((a, b) => stamp(a.body.client_ts) - stamp(b.body.client_ts));
    },
    dropPosition: (profileId, key) => exclusive(async () => ((await activeProfile()) === profileId ? own(profileId).delete(key) : undefined)),
    async since(profileId) {
      const value = await own(profileId).get('since');
      return typeof value === 'string' ? value : null;
    },
    // nothing is written for a profile that is no longer the one on this browser (it would bring its store back)
    setSince: (profileId, token) => exclusive(async () => ((await activeProfile()) === profileId ? own(profileId).set('since', token) : undefined)),
    async bindSession(token) {
      const hash = await digest(token);
      await exclusive(() => active.set('session', hash));
    },
    async sessionMatches(token) {
      const bound = await active.get('session');
      return typeof bound === 'string' ? bound === (await digest(token)) : null;
    }
  };
}

const absent = () => new Response('not found', { status: 404, headers: { 'cache-control': 'no-store' } });

/** The answer of the worker to a request for the bytes of a copy: the active profile's copy with the
 * headers it was served with, or a plain 404 (never the network). A path that is odd (an encoded
 * slash, a `.` or `..` segment, an empty one) is not a copy. */
export async function serveCopyEntry(env: OfflineEnv, url: URL): Promise<Response> {
  const path = url.pathname;
  if (/%2f/i.test(path) || /%5c/i.test(path)) return absent();
  let decoded: string;
  try {
    decoded = decodeURIComponent(path);
  } catch {
    return absent();
  }
  const match = COPY_PATH.exec(path);
  if (!match || decoded.includes('//') || decoded.split('/').some((s) => s === '.' || s === '..')) return absent();
  const profileId = await env.openKv(ACTIVE_KV).get('profile');
  if (typeof profileId !== 'string' || profileId === '') return absent();
  if (!(await env.caches.has(copyCacheName(profileId)))) return absent();
  const cache = await env.caches.open(copyCacheName(profileId));
  return (await cache.match(path)) ?? absent();
}
