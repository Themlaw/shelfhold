// PR #396 (M5 L16b), contradicteur: the holes found in the offline copies, each a red test that
// turns green once fixed (`it.fails`: it passes today because the assertion fails).
//
// C2: the queue is sent for the profile that was active when the sync started, even once the browser
//     has changed hands in another tab (sign-out, then a child signs in): the next positions leave
//     with the session of the browser, i.e. into the child's progress.
// C3: two positions queued at once (two tabs, or a reader whose failing writes overlap on a slow
//     network) are read then written without a transaction: the older one can replace the later one.
// C4: `entries()` of the IndexedDB store reads the keys and the values in two transactions; a write
//     between them (another tab keeping a copy) pairs keys with the wrong values, and the eviction by
//     `?since=` misses a copy the server named, while the token moves past it.
// C5: a sync whose `?since=` cannot be read (the server unreachable while the browser says it is
//     online, or busy) schedules nothing when no position waits: no `online` event will come, so
//     the eviction waits for the next load of the SPA, and the copy stays readable offline.
// C6 (not 🔒, the use of the copy): an EPUB is kept as its package and the documents of its spine
//     only; the images, styles and fonts its documents and its manifest name are never fetched, so
//     the copy opens with no network but without them (the engine asks `<base><name>`: a 404).
import { describe, expect, it } from 'vitest';
import type { PositionIn, PublicationOut } from '#lib/api/types.gen.ts';
import { copyUrl } from './env.ts';
import { idbEnv } from './idb.ts';
import { keepCopy } from './keep.ts';
import { createOfflineStore } from './store.ts';
import { syncOffline, type OfflineApi } from './sync.ts';
import { memoryEnv } from './testing.ts';

const ADULT = '01JADULT00000000000000000A';
const CHILD = '01JCHILD00000000000000000C';
const A = '01JCARRIERA00000000000000A';
const B = '01JCARRIERB00000000000000B';
const WORK_A = '01JWORKA0000000000000000A';
const WORK_B = '01JWORKB0000000000000000B';

const position = (carrier: string, ts: string, index = 3): PositionIn => ({
  carrier_type: 'content',
  carrier_id: carrier,
  client_ts: ts,
  locator: { v: 1, type: 'page', index, count: 30 },
  device_id: 'web-test'
});

function publicationOf(carrier: string, work: string): PublicationOut {
  return {
    carrier_type: 'content',
    carrier_id: carrier,
    work_id: work, series_id: work, edition_id: 'E1',
    scope: 'full',
    kind: 'pages',
    title: carrier,
    number: null,
    language: null,
    reading_direction: 'ltr',
    expires_at: '2026-11-19T10:10:00Z',
    pages: [],
    spine: [],
    package: null
  };
}

const meta = (carrier: string, work: string) => ({
  carrierType: 'content' as const,
  carrierId: carrier,
  workId: work,
  title: carrier,
  publication: publicationOf(carrier, work)
});

describe('#396 contradicteur', () => {
  // req: EF-LEC-007, EF-USR-006
  it('C2: a queue is not sent once another profile is active on the browser (no reading lands in the child progress)', async () => {
    const env = memoryEnv();
    const store = createOfflineStore(env);
    await store.activate(ADULT);
    await store.queuePosition(ADULT, position(A, '2026-11-19T08:00:00Z'));
    await store.queuePosition(ADULT, position(B, '2026-11-19T09:00:00Z'));
    const sent: PositionIn[] = [];
    const api: OfflineApi = {
      works: async () => ({ status: 200, since: 'next', removed: [] }),
      publication: async () => ({ status: 404 }),
      async putPosition(body) {
        sent.push(body);
        if (sent.length === 1) {
          // meanwhile, in another tab: the adult signs out, the child signs in (the cookie is hers now)
          await store.wipeAll();
          await store.activate(CHILD);
        }
        return { status: 200 };
      }
    };
    await syncOffline({ store, profileId: ADULT, api });
    expect(sent.map((p) => p.carrier_id)).toEqual([A]); // today B leaves too, with the child's session
  });

  // req: EF-LEC-007
  it('C3: two positions queued at once keep the later reading', async () => {
    const env = memoryEnv();
    const store = createOfflineStore(env);
    await store.activate(ADULT);
    const later = position(A, '2026-11-19T09:30:00Z', 20);
    const earlier = position(A, '2026-11-19T09:00:00Z', 5);
    await Promise.all([store.queuePosition(ADULT, later), store.queuePosition(ADULT, earlier)]);
    const [kept] = await store.queued(ADULT);
    expect(kept?.body.client_ts).toBe(later.client_ts); // today: the earlier one, page 5 instead of 20
  });

  // req: EF-LEC-012, EF-INT-001
  it('C4: an eviction named by ?since= is not missed when another tab keeps a copy at the same moment', async () => {
    const memory = memoryEnv();
    const fake = fakeIndexedDb();
    const env = idbEnv({ indexedDB: fake.factory, caches: memory.caches });
    const store = createOfflineStore(env);
    await store.activate(ADULT);
    await store.keep(ADULT, meta(B, WORK_B), [{ url: copyUrl('content', B, 'p1.png'), response: new Response('b') }]);
    // between the keys and the values of `entries()`, the other tab writes the meta of a new copy
    fake.between = () =>
      fake.write(`shelfhold-offline-p-${ADULT}`, `copy:content:${A}`, {
        ...meta(A, WORK_A),
        keptAt: '2026-11-19T10:00:00Z'
      });
    const gone = await store.evictWorks(ADULT, [WORK_B]);
    expect(gone).toEqual([WORK_B]); // today: [] and the copy of B stays readable
  });

  // req: EF-LEC-012, EF-INT-001
  it('C5: a sync that could not ask ?since= comes back later, even with no position waiting', async () => {
    const env = memoryEnv();
    const store = createOfflineStore(env);
    await store.activate(ADULT);
    await store.keep(ADULT, meta(B, WORK_B), [{ url: copyUrl('content', B, 'p1.png'), response: new Response('b') }]);
    await store.setSince(ADULT, 'old');
    const down: OfflineApi = {
      works: async () => {
        throw new TypeError('network'); // the server cannot be reached; `navigator.onLine` is still true
      },
      publication: async () => ({ status: 404 }),
      putPosition: async () => ({ status: 200 })
    };
    const busy: OfflineApi = {
      ...down,
      works: async () => ({ status: 503 })
    };
    const unreachable = await syncOffline({
      store,
      profileId: ADULT,
      api: down
    });
    const overloaded = await syncOffline({
      store,
      profileId: ADULT,
      api: busy
    });
    expect([unreachable.retryAfterMs, overloaded.retryAfterMs].every((ms) => ms !== null && ms > 0)).toBe(true); // today: null, null
  });

  // req: EF-LEC-012
  it('C6: an EPUB copy holds the resources its manifest names, not only its spine', async () => {
    const env = memoryEnv();
    const store = createOfflineStore(env);
    await store.activate(ADULT);
    const opf = `<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest>
      <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
      <item id="css" href="style.css" media-type="text/css"/>
      <item id="img" href="images/plate.png" media-type="image/png"/>
    </manifest><spine><itemref idref="c1"/></spine></package>`;
    const epub: PublicationOut = {
      ...publicationOf(A, WORK_A),
      kind: 'epub',
      package: '/content/TICKET/OEBPS/content.opf',
      spine: [
        {
          index: 1,
          href: '/content/TICKET/OEBPS/c1.xhtml',
          media_type: 'application/xhtml+xml'
        }
      ]
    };
    const asked: string[] = [];
    const api = {
      works: async () => ({ status: 200, since: 'tok', removed: [] }),
      publication: async () => ({ status: 200, publication: epub }),
      putPosition: async () => ({ status: 200 }),
      entry: async (url: string) => (asked.push(url), new Response(url.endsWith('.opf') ? opf : 'x', { status: 200 }))
    };
    await keepCopy({
      store,
      profileId: ADULT,
      api,
      carrierType: 'content',
      carrierId: A
    });
    expect(asked).toContain('/content/TICKET/OEBPS/images/plate.png'); // today: only the OPF and c1.xhtml
  });
});

/** Enough of IndexedDB for `idb.ts`: one object store per database, requests answered in a later
 * microtask, and a hook that runs between a `getAllKeys` and the next request (another tab). */
function fakeIndexedDb() {
  const dbs = new Map<string, Map<string, unknown>>();
  const state: {
    between: (() => void) | null;
    write: (db: string, key: string, value: unknown) => void;
    factory: IDBFactory;
  } = {
    between: null,
    write: (db, key, value) => void dbs.get(db)?.set(key, value),
    factory: null as unknown as IDBFactory
  };
  type Req = {
    result?: unknown;
    error?: unknown;
    onsuccess?: () => void;
    onerror?: () => void;
    onupgradeneeded?: () => void;
    onblocked?: () => void;
  };
  const answer = (compute: () => unknown, after?: () => void): Req => {
    const request: Req = {};
    queueMicrotask(() => {
      request.result = compute();
      after?.();
      request.onsuccess?.();
    });
    return request;
  };
  const sorted = (data: Map<string, unknown>) => [...data.keys()].sort();
  state.factory = {
    open(name: string) {
      const request: Req = {};
      queueMicrotask(() => {
        const fresh = !dbs.has(name);
        if (fresh) dbs.set(name, new Map());
        const data = dbs.get(name)!;
        request.result = {
          createObjectStore: () => undefined,
          close: () => undefined,
          transaction: () => ({
            objectStore: () => ({
              get: (key: string) => answer(() => structuredClone(data.get(key))),
              put: (value: unknown, key: string) => answer(() => void data.set(key, structuredClone(value))),
              delete: (key: string) => answer(() => void data.delete(key)),
              getAllKeys: () =>
                answer(
                  () => sorted(data),
                  () => {
                    const hook = state.between;
                    state.between = null;
                    hook?.();
                  }
                ),
              getAll: () => answer(() => sorted(data).map((key) => structuredClone(data.get(key))))
            })
          })
        };
        if (fresh) request.onupgradeneeded?.();
        request.onsuccess?.();
      });
      return request;
    },
    deleteDatabase(name: string) {
      return answer(() => void dbs.delete(name));
    }
  } as unknown as IDBFactory;
  return state;
}
