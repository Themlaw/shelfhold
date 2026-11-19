// PR #396 (M5 L16b), second contradicteur: what the fixes of C2 to C6 and M6 still leave open. Each
// is a red test that turns green once fixed (`it.fails`: it passes today because the assertion fails).
//
// S1 (🔒, C2 not closed): the check before each send reads the *profile* the store says is active,
//     not the *session* the request goes with. The cookie changes as soon as another profile signs in
//     (in another tab, on the server's `/login`), well before that tab's SPA activates its profile:
//     in between, the previous profile's queue still leaves, with the child's cookie and anti-forgery
//     token, into the child's progress. The binding of M6 (`sessionMatches`) is what tells it.
// S2 (integrity, M6 fix): on the first load after another profile signs in, the layout starts the
//     guard (`guardOfflineSession(child)`) and the sync (`startOfflineSync(child)`) together. The
//     guard read the previous profile as active, so its `wipeAll` lands after `activate(child)` and
//     wipes it: no profile is active for the rest of the visit (no sync; "keep" fails; a position the
//     reader queues is refused).
// S3 (🔒 availability of sign-out, C3 fix): `keep` holds the lock (chain and Web Lock) for the whole
//     download. Sign-out awaits `wipeAll` before its request: while a big book downloads (or a fetch
//     that never ends), the button does nothing; the session stays and the copies too.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { KeepApi } from './keep.ts';
import { createOfflineStore } from './store.ts';
import { memoryEnv, type MemoryEnv } from './testing.ts';

const held = vi.hoisted(() => ({ env: null as unknown as MemoryEnv }));
vi.mock('./idb.ts', () => ({ idbEnv: () => held.env }));

const { guardOfflineSession, offlineStore, startOfflineSync, wipeOfflineCopies } = await import('./runtime.ts');
const ADULT = '01JADULT00000000000000000A';
const CHILD = '01JCHILD00000000000000000C';
const cookie = (token: string) => void (document.cookie = `csrf=${token}`);

const api = (over: Partial<KeepApi> = {}): KeepApi => ({
  works: vi.fn(async () => ({ status: 200, since: 'tok', removed: [] })),
  publication: vi.fn(async () => ({ status: 404 })),
  putPosition: vi.fn(async () => ({ status: 200 })),
  entry: vi.fn(async () => new Response('x')),
  ...over
});

held.env = memoryEnv();
beforeEach(async () => {
  await createOfflineStore(held.env).wipeAll();
});
afterEach(() => vi.useRealTimers());

describe('second contradicteur (#396)', () => {
  // req: EF-LEC-007, ENF-SEC-010
  it('S1: a queue is not sent once the session of the browser is another one, before any tab activates it', async () => {
    const store = offlineStore();
    const target = new EventTarget();
    const put = vi.fn(async () => ({ status: 503 })); // first pass: busy, the position waits
    cookie('tok-adult');
    const stop = startOfflineSync(ADULT, { api: api({ putPosition: put }), target: target as never });
    await vi.waitFor(async () => expect(await store.sessionMatches('tok-adult')).toBe(true));
    await store.queuePosition(ADULT, { carrier_type: 'content', carrier_id: 'C1', client_ts: '2026-11-19T09:00:00Z', locator: { v: 1 } });
    // another tab: the adult's session ended, the child signed in on /login; its SPA has not activated yet
    cookie('tok-child');
    put.mockClear();
    target.dispatchEvent(new Event('online'));
    await new Promise((r) => setTimeout(r, 50));
    stop();
    expect(put).not.toHaveBeenCalled(); // today: sent with the child's cookie and token
  });

  // req: EF-LEC-012
  it('S2: the first load after another profile signed in leaves that profile active', async () => {
    const store = offlineStore();
    await store.activate(ADULT);
    cookie('tok-adult');
    await store.bindSession('tok-adult');
    cookie('tok-child'); // the child signed in; /me answered with the child (the layout's $effect)
    void guardOfflineSession(CHILD);
    const stop = startOfflineSync(CHILD, { api: api(), target: new EventTarget() as never });
    await new Promise((r) => setTimeout(r, 50));
    stop();
    expect(await store.activeProfile()).toBe(CHILD); // today: null, the guard wiped the child's activation
  });

  // req: EF-LEC-012, ENF-SEC-010
  it('S3: sign-out does not wait for a copy that is still downloading', async () => {
    const store = offlineStore();
    await store.activate(ADULT);
    const never: AsyncIterable<{ url: string; response: Response }> = {
      [Symbol.asyncIterator]: () => ({ next: () => new Promise(() => undefined) }) // a fetch that never ends
    };
    void store.keep(ADULT, { carrierType: 'content', carrierId: 'C1', workId: 'W1', title: 't', publication: {} as never }, never).catch(() => undefined);
    const done = await Promise.race([wipeOfflineCopies().then(() => 'wiped'), new Promise((r) => setTimeout(() => r('stuck'), 200))]);
    expect(done).toBe('wiped'); // today: stuck behind the download
  });
});
