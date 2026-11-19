// When the offline sync runs (M5 L16b): at start, when the network comes back, after the delay a
// busy server asked for; and sign-out leaves nothing (EF-LEC-012, EF-LEC-007).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { KeepApi } from './keep.ts';
import { createOfflineStore, type OfflineStore } from './store.ts';
import { memoryEnv, type MemoryEnv } from './testing.ts';

const held = vi.hoisted(() => ({ env: null as unknown as MemoryEnv }));
vi.mock('./idb.ts', () => ({ idbEnv: () => held.env }));

const { offlineStore, startOfflineSync, wipeOfflineCopies } = await import('./runtime.ts');
const ALICE = '01JALICE00000000000000000A';

const api = (over: Partial<KeepApi> = {}): KeepApi => ({
  works: vi.fn(async () => ({ status: 200, since: 'tok', removed: [] })),
  publication: vi.fn(async () => ({ status: 404 })),
  putPosition: vi.fn(async () => ({ status: 200 })),
  entry: vi.fn(async () => new Response('x')),
  ...over
});

held.env = memoryEnv(); // the module keeps its store: one environment for the file, wiped between tests
beforeEach(async () => {
  await createOfflineStore(held.env).wipeAll();
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

describe('startOfflineSync', () => {
  // req: EF-LEC-007
  it('activates the profile, sends the queue, and comes back after Retry-After when the server is busy', async () => {
    const store: OfflineStore = offlineStore();
    const target = new EventTarget();
    const put = vi.fn().mockResolvedValueOnce({ status: 503, retryAfter: 5 }).mockResolvedValue({ status: 200 });
    const stop = startOfflineSync(ALICE, { api: api({ putPosition: put }), target: target as never });
    await vi.waitFor(async () => expect(await store.activeProfile()).toBe(ALICE));
    await store.queuePosition(ALICE, { carrier_type: 'content', carrier_id: 'C1', client_ts: '2026-11-19T09:00:00Z', locator: { v: 1 } });
    target.dispatchEvent(new Event('online'));
    await vi.waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(await store.queued(ALICE)).toHaveLength(1); // kept
    await vi.advanceTimersByTimeAsync(5000);
    await vi.waitFor(() => expect(put).toHaveBeenCalledTimes(2));
    expect(await store.queued(ALICE)).toEqual([]);
    stop();
  });

  // req: EF-LEC-012
  it('stops listening and waiting once stopped', async () => {
    const target = new EventTarget();
    const works = vi.fn(async () => ({ status: 200, since: 'tok', removed: [] }));
    const stop = startOfflineSync(ALICE, { api: api({ works }), target: target as never });
    await vi.waitFor(() => expect(works).toHaveBeenCalled());
    stop();
    const calls = works.mock.calls.length;
    target.dispatchEvent(new Event('online'));
    await vi.advanceTimersByTimeAsync(60_000);
    expect(works.mock.calls.length).toBe(calls);
  });
});

describe('wipeOfflineCopies', () => {
  // req: EF-LEC-012
  it('wipes every copy and never throws', async () => {
    const store = createOfflineStore(held.env);
    await store.activate(ALICE);
    await store.keep(ALICE, { carrierType: 'content', carrierId: 'C1', workId: 'W1', title: 't', publication: {} as never }, [
      { url: '/content/offline.content.C1/p1.png', response: new Response('x') }
    ]);
    await wipeOfflineCopies();
    expect(await held.env.caches.keys()).toEqual([]);
    held.env.deleteKv = async () => {
      throw new Error('no storage');
    };
    await store.activate(ALICE);
    await expect(wipeOfflineCopies()).resolves.toBeUndefined();
  });
});
