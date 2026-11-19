// Back online (M5 L16b, EF-LEC-007, EF-LEC-012, EF-INT-001): the positions read offline go to the
// server with the `client_ts` of the moment they were read (the merge of L07), and what left the
// profile's view since the last token is evicted by `?since=`.
import { describe, expect, it, vi } from 'vitest';
import type { PositionIn, PublicationOut } from '#lib/api/types.gen.ts';
import { copyUrl } from './env.ts';
import { createOfflineStore } from './store.ts';
import { createQueueingPut, syncOffline, type OfflineApi } from './sync.ts';
import { memoryEnv } from './testing.ts';

const ALICE = '01JALICE00000000000000000A';
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
    carrier_type: 'content', carrier_id: carrier, work_id: work, series_id: work, scope: 'full', kind: 'pages', title: carrier,
    number: null, language: null, reading_direction: 'ltr', expires_at: '2026-11-19T10:10:00Z', pages: [], spine: [], package: null
  };
}

async function setup() {
  const env = memoryEnv();
  const store = createOfflineStore(env);
  await store.activate(ALICE);
  for (const [carrier, work] of [[A, WORK_A], [B, WORK_B]] as const) {
    await store.keep(ALICE, { carrierType: 'content', carrierId: carrier, workId: work, title: carrier, publication: publicationOf(carrier, work) }, [
      { url: copyUrl('content', carrier, 'p1.png'), response: new Response('x') }
    ]);
  }
  return { env, store };
}

function api(over: Partial<OfflineApi> = {}): OfflineApi {
  return {
    works: vi.fn(async () => ({ status: 200, since: 'next', removed: [] })),
    publication: vi.fn(async (_t: string, id: string) => ({ status: 200, publication: publicationOf(id, id === A ? WORK_A : WORK_B) })),
    putPosition: vi.fn(async () => ({ status: 200 })),
    ...over
  };
}

describe('offline positions merged on reconnect', () => {
  // req: EF-LEC-007
  it('offline positions merged on reconnect: sent with the client_ts they were read at, oldest first', async () => {
    const { store } = await setup();
    await store.queuePosition(ALICE, position(A, '2026-11-19T09:00:00Z', 5));
    await store.queuePosition(ALICE, position(B, '2026-11-19T08:00:00Z', 2));
    const sent: PositionIn[] = [];
    const result = await syncOffline({ store, profileId: ALICE, api: api({ putPosition: async (body) => (sent.push(body), { status: 200 }) }) });
    expect(sent.map((b) => [b.carrier_id, b.client_ts])).toEqual([
      [B, '2026-11-19T08:00:00Z'],
      [A, '2026-11-19T09:00:00Z']
    ]);
    expect(await store.queued(ALICE)).toEqual([]);
    expect(result.sent).toBe(2);
  });

  // req: EF-LEC-007
  it('keeps only the latest position of a carrier (a reading is a place, not a log)', async () => {
    const { store } = await setup();
    await store.queuePosition(ALICE, position(A, '2026-11-19T09:00:00Z', 1));
    await store.queuePosition(ALICE, position(A, '2026-11-19T09:05:00Z', 7));
    await store.queuePosition(ALICE, position(A, '2026-11-19T09:02:00Z', 4)); // a late write never wins
    const queued = await store.queued(ALICE);
    expect(queued).toHaveLength(1);
    expect(queued[0]!.body.locator).toMatchObject({ index: 7 });
  });

  // req: EF-LEC-007
  it('503 progress_busy: the position is kept and the sync asks to come back after Retry-After', async () => {
    const { store } = await setup();
    await store.queuePosition(ALICE, position(A, '2026-11-19T09:00:00Z'));
    const result = await syncOffline({ store, profileId: ALICE, api: api({ putPosition: async () => ({ status: 503, retryAfter: 7 }) }) });
    expect(result.retryAfterMs).toBe(7000);
    expect(await store.queued(ALICE)).toHaveLength(1); // never thrown away
    expect(result.sent).toBe(0);
  });

  // req: EF-LEC-007
  it('a network failure keeps the queue and retries later, with a default delay', async () => {
    const { store } = await setup();
    await store.queuePosition(ALICE, position(A, '2026-11-19T09:00:00Z'));
    const result = await syncOffline({ store, profileId: ALICE, api: api({ putPosition: async () => { throw new TypeError('offline'); } }) });
    expect(await store.queued(ALICE)).toHaveLength(1);
    expect(result.retryAfterMs).toBeGreaterThan(0);
  });

  // req: EF-LEC-007
  it('a position the server refuses for good (404, 409, 422) is dropped; the next one is still sent', async () => {
    const { store } = await setup();
    await store.queuePosition(ALICE, position(A, '2026-11-19T09:00:00Z'));
    await store.queuePosition(ALICE, position(B, '2026-11-19T09:01:00Z'));
    const answers = [404, 200];
    const result = await syncOffline({ store, profileId: ALICE, api: api({ putPosition: async () => ({ status: answers.shift()! }) }) });
    expect(await store.queued(ALICE)).toEqual([]);
    expect(result.sent).toBe(1);
  });

  // req: EF-LEC-007
  it('the queueing put sends when it can and queues on a network failure or a busy answer', async () => {
    const { store } = await setup();
    const put = vi.fn<(b: PositionIn) => Promise<{ status: number; retryAfter?: number }>>();
    const queueing = createQueueingPut({ store, profileId: ALICE, put });
    put.mockResolvedValueOnce({ status: 200 });
    await queueing(position(A, '2026-11-19T09:00:00Z'));
    expect(await store.queued(ALICE)).toEqual([]);
    put.mockRejectedValueOnce(new TypeError('offline'));
    await queueing(position(A, '2026-11-19T09:01:00Z'));
    put.mockResolvedValueOnce({ status: 503, retryAfter: 2 });
    await queueing(position(B, '2026-11-19T09:02:00Z'));
    expect((await store.queued(ALICE)).map((q) => q.body.carrier_id).sort()).toEqual([A, B]);
  });
});

describe('what left the view is evicted at reconnection by ?since=', () => {
  // req: EF-LEC-012, EF-INT-001
  it('test_offline_copy_evicted_after_reclassification: the Works named by the server are evicted', async () => {
    const { store } = await setup();
    await store.setSince(ALICE, 'old');
    const works = vi.fn(async (since: string | null) => ({ status: 200, since: 'fresh', removed: since === 'old' ? [WORK_A, '01JNEVERKEPT000000000000N'] : [] }));
    const result = await syncOffline({ store, profileId: ALICE, api: api({ works }) });
    expect(result.evicted).toEqual([WORK_A]);
    expect((await store.copies(ALICE)).map((c) => c.workId)).toEqual([WORK_B]);
    expect(await store.since(ALICE)).toBe('fresh');
  });

  // req: EF-LEC-012, EF-USR-006
  it('resync_required (410): each copy is asked again; a 404 is evicted, a 200 is kept; nothing is learnt of others', async () => {
    const { store } = await setup();
    await store.setSince(ALICE, 'old');
    const publication = vi.fn(async (_t: string, id: string) => (id === A ? { status: 404 } : { status: 200, publication: publicationOf(id, WORK_B) }));
    const works = vi.fn(async (since: string | null) => (since === 'old' ? { status: 410 } : { status: 200, since: 'fresh', removed: [] }));
    const result = await syncOffline({ store, profileId: ALICE, api: api({ works, publication }) });
    expect(result.evicted).toEqual([WORK_A]);
    expect((await store.copies(ALICE)).map((c) => c.workId)).toEqual([WORK_B]);
    expect(await store.since(ALICE)).toBe('fresh');
  });

  // req: EF-LEC-012, ENF-SEC-010
  it('401 or 403 (session or token revoked, access lost): every copy of the profile is wiped, queue included', async () => {
    for (const status of [401, 403]) {
      const { store, env } = await setup();
      await store.setSince(ALICE, 'old');
      await store.queuePosition(ALICE, position(A, '2026-11-19T09:00:00Z'));
      const result = await syncOffline({ store, profileId: ALICE, api: api({ works: async () => ({ status }) }) });
      expect(result.wiped).toBe(true);
      expect(await store.copies(ALICE)).toEqual([]);
      expect(await store.queued(ALICE)).toEqual([]);
      expect(await env.caches.keys()).toEqual([]);
    }
  });

  // req: EF-LEC-012
  it('offline: nothing is evicted and nothing is wiped', async () => {
    const { store } = await setup();
    await store.setSince(ALICE, 'old');
    const result = await syncOffline({ store, profileId: ALICE, api: api({ works: async () => { throw new TypeError('offline'); } }) });
    expect(result.wiped).toBe(false);
    expect(await store.copies(ALICE)).toHaveLength(2);
    expect(await store.since(ALICE)).toBe('old');
  });

  // req: EF-LEC-012
  it('a profile that is not the active one is never synchronised (a stale tab cannot wipe or send for another)', async () => {
    const { store } = await setup();
    const calls = api();
    const result = await syncOffline({ store, profileId: '01JSOMEONEELSE00000000000S', api: calls });
    expect(result).toMatchObject({ sent: 0, evicted: [], wiped: false });
    expect(calls.works).not.toHaveBeenCalled();
  });
});

describe('what must not be lost or missed (review)', () => {
  // req: EF-LEC-012, EF-USR-006
  it('410 then a copy that could not be asked (503): the token does not move past it', async () => {
    const { store } = await setup();
    await store.setSince(ALICE, 'old');
    const works = vi.fn(async (since: string | null) => (since === 'old' ? { status: 410 } : { status: 200, since: 'fresh', removed: [] }));
    const publication = vi.fn(async (_t: string, id: string) => (id === A ? { status: 503 } : { status: 200, publication: publicationOf(id, WORK_B) }));
    await syncOffline({ store, profileId: ALICE, api: api({ works, publication }) });
    expect(await store.since(ALICE)).toBe('old'); // the next connection resynchronises again
  });

  // req: EF-LEC-012
  it('400 (a token this instance does not know) is a resynchronisation too', async () => {
    const { store } = await setup();
    await store.setSince(ALICE, 'old');
    const works = vi.fn(async (since: string | null) => (since === 'old' ? { status: 400 } : { status: 200, since: 'fresh', removed: [] }));
    const publication = vi.fn(async (_t: string, id: string) => (id === A ? { status: 404 } : { status: 200, publication: publicationOf(id, WORK_B) }));
    const result = await syncOffline({ store, profileId: ALICE, api: api({ works, publication }) });
    expect(result.evicted).toEqual([WORK_A]);
    expect(await store.since(ALICE)).toBe('fresh');
  });

  // req: EF-LEC-007
  it('403 csrf_failed keeps the position and the copies; any other 403 is a lost access', async () => {
    const { store } = await setup();
    await store.queuePosition(ALICE, position(A, '2026-11-19T09:00:00Z'));
    const kept = await syncOffline({ store, profileId: ALICE, api: api({ putPosition: async () => ({ status: 403, code: 'csrf_failed' }) }) });
    expect(kept.wiped).toBe(false);
    expect(await store.queued(ALICE)).toHaveLength(1);
    expect(await store.copies(ALICE)).toHaveLength(2);
    const lost = await syncOffline({ store, profileId: ALICE, api: api({ putPosition: async () => ({ status: 403, code: 'forbidden' }) }) });
    expect(lost.wiped).toBe(true);
  });

  // req: EF-LEC-007
  it('removing a copy keeps the positions read offline', async () => {
    const { store } = await setup();
    await store.queuePosition(ALICE, position(A, '2026-11-19T09:00:00Z'));
    await store.evictCarrier(ALICE, 'content', A);
    expect(await store.queued(ALICE)).toHaveLength(1);
  });

  // req: EF-LEC-012
  it('a copy is not kept when the token could not be renewed', async () => {
    const { keepCopy } = await import('./keep.ts');
    const { store } = await setup();
    await expect(keepCopy({ store, profileId: ALICE, api: { ...api({ works: async () => ({ status: 503 }) }), entry: async () => new Response('x') }, carrierType: 'unit', carrierId: 'U' })).rejects.toMatchObject({ reason: 'download_failed' });
  });
});

