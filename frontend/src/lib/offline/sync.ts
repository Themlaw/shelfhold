// Back online (M5 L16b, EF-LEC-007, EF-LEC-012, EF-INT-001): send the positions read offline and
// evict what left the profile's view.
//
// Positions: each one goes with the `client_ts` it was read at, oldest first, so the server merges
// them as L07 says (the last reading wins, a late write never replaces a later one). A busy server
// (503, with `Retry-After`, L22a) or a network failure **keeps** the position and says when to come
// back: a position is never thrown away for being early. Only an answer that will never change
// (404, 409, 422: the carrier is gone or the write refused) drops it.
//
// Copies: `?since=` of the list of Works names every Work that left this profile's view since the
// token (deleted, purged, reclassified): its copy is evicted. The token is signed and bound to the
// profile's scope, so it cannot be carried to another profile; a token too old, or one the server
// does not recognise (410), is a resynchronisation: each copy is asked again and answers 404 for
// what the profile may not see, which tells nothing about any other Work. A 401 or 403 is a
// session or a token revoked, or access lost: everything of the profile is wiped.
import type { PositionIn, PublicationOut } from '#lib/api/types.gen.ts';
import type { OfflineStore } from './store.ts';

export interface OfflineApi {
  works(since: string | null): Promise<{ status: number; since?: string; removed?: string[] }>;
  publication(carrierType: string, carrierId: string): Promise<{ status: number; publication?: PublicationOut }>;
  putPosition(body: PositionIn): Promise<{ status: number; retryAfter?: number }>;
}

export interface SyncResult {
  sent: number;
  evicted: string[];
  wiped: boolean;
  /** When to come back (a busy server, a network that is still down); null when nothing waits. */
  retryAfterMs: number | null;
}

export const DEFAULT_RETRY_MS = 30_000;
const MAX_RETRY_MS = 15 * 60_000;
const LOST = new Set([401, 403]);
const retryable = (status: number) => status === 429 || status >= 500;
const delay = (seconds: number | undefined) =>
  seconds !== undefined && Number.isFinite(seconds) && seconds > 0 ? Math.min(seconds * 1000, MAX_RETRY_MS) : DEFAULT_RETRY_MS;

interface Options {
  store: OfflineStore;
  profileId: string;
  api: OfflineApi;
}

async function flush({ store, profileId, api }: Options, result: SyncResult): Promise<void> {
  for (const waiting of await store.queued(profileId)) {
    let answer: { status: number; retryAfter?: number };
    try {
      answer = await api.putPosition(waiting.body);
    } catch {
      result.retryAfterMs = DEFAULT_RETRY_MS; // offline: the queue stays as it is
      return;
    }
    if (answer.status >= 200 && answer.status < 300) {
      await store.dropPosition(profileId, waiting.key);
      result.sent += 1;
    } else if (LOST.has(answer.status)) {
      await store.wipe(profileId);
      result.wiped = true;
      return;
    } else if (retryable(answer.status)) {
      result.retryAfterMs = delay(answer.retryAfter); // kept
      return;
    } else {
      await store.dropPosition(profileId, waiting.key); // a refusal that will not change
    }
  }
}

/** The removals since the token, or a resynchronisation. Never evicts on a failure it cannot read. */
export async function syncSince({ store, profileId, api }: Options, result: Pick<SyncResult, 'evicted' | 'wiped'>): Promise<void> {
  const lost = async () => {
    await store.wipe(profileId);
    result.wiped = true;
  };
  try {
    const token = await store.since(profileId);
    const answer = await api.works(token);
    if (LOST.has(answer.status)) return await lost();
    if (answer.status === 200 && token !== null && answer.since) {
      result.evicted.push(...(await store.evictWorks(profileId, answer.removed ?? [])));
      await store.setSince(profileId, answer.since);
      return;
    }
    if (answer.status !== 410 && !(answer.status === 200 && token === null)) return; // not readable now: try again later
    // a resynchronisation: take a token first, so that nothing that changes while the copies are asked again is missed
    const fresh = token === null && answer.since ? answer : await api.works(null);
    if (LOST.has(fresh.status)) return await lost();
    if (fresh.status !== 200 || !fresh.since) return;
    for (const copy of await store.copies(profileId)) {
      const asked = await api.publication(copy.carrierType, copy.carrierId);
      if (LOST.has(asked.status)) return await lost();
      if (asked.status === 404) {
        await store.evictCarrier(profileId, copy.carrierType, copy.carrierId);
        if (!result.evicted.includes(copy.workId)) result.evicted.push(copy.workId);
      }
    }
    await store.setSince(profileId, fresh.since);
  } catch {
    // offline: nothing is evicted and nothing is wiped; the next reconnection asks again
  }
}

export async function syncOffline(options: Options): Promise<SyncResult> {
  const result: SyncResult = { sent: 0, evicted: [], wiped: false, retryAfterMs: null };
  // a stale tab, or someone else's session, never sends nor evicts for the profile on the browser
  if ((await options.store.activeProfile()) !== options.profileId) return result;
  await flush(options, result);
  if (!result.wiped) await syncSince(options, result);
  return result;
}

/** The `put` the reader's position sender uses for a copy: it sends, and queues what cannot go. */
export function createQueueingPut(options: {
  store: OfflineStore;
  profileId: string;
  put: (body: PositionIn) => Promise<{ status: number; retryAfter?: number }>;
}): (body: PositionIn) => Promise<void> {
  return async (body) => {
    try {
      const answer = await options.put(body);
      if (retryable(answer.status)) await options.store.queuePosition(options.profileId, body);
    } catch {
      await options.store.queuePosition(options.profileId, body);
    }
  };
}
