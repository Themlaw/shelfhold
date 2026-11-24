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
// token (deleted, purged, reclassified): its copy is evicted. `removed_editions` names the Editions
// that left it while their Work stayed (another Edition carries it): the copy of each is evicted. The token is signed and bound to the
// profile's scope, so it cannot be carried to a profile of another scope; a token too old, or one the server
// does not recognise (410), is a resynchronisation: each copy is asked again and answers 404 for
// what the profile may not see, which tells nothing about any other Work. A 401 or 403 is a
// session or a token revoked, or access lost: everything of the profile is wiped.
import type { PositionIn, PublicationOut } from '#lib/api/types.gen.ts';
import type { OfflineStore } from './store.ts';

export interface OfflineApi {
  works(since: string | null): Promise<{ status: number; since?: string; removed?: string[]; removedEditions?: string[] }>;
  publication(carrierType: string, carrierId: string): Promise<{ status: number; publication?: PublicationOut }>;
  putPosition(body: PositionIn): Promise<{ status: number; retryAfter?: number; code?: string }>;
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
// a 403 `csrf_failed` is a stale token, not a lost access: the position waits
const isLost = (status: number, code?: string) => status === 401 || (status === 403 && code !== 'csrf_failed');
const retryable = (status: number) => status === 429 || status >= 500;
const delay = (seconds: number | undefined) =>
  seconds !== undefined && Number.isFinite(seconds) && seconds > 0 ? Math.min(seconds * 1000, MAX_RETRY_MS) : DEFAULT_RETRY_MS;

interface Options {
  store: OfflineStore;
  profileId: string;
  api: OfflineApi;
  /** Whether the session the requests go with is the one the profile was bound to (the cookie of the
   * browser changes as soon as another profile signs in, before any tab has activated it). */
  sessionOk?: () => Promise<boolean>;
}

async function flush({ store, profileId, api, sessionOk }: Options, result: SyncResult): Promise<void> {
  for (const waiting of await store.queued(profileId)) {
    // the browser may have changed hands in another tab since the last one left: the next position would
    // go with the session of whoever is there now. Nothing leaves for a profile that is not the active one.
    if ((await store.activeProfile()) !== profileId) return;
    if (sessionOk && !(await sessionOk())) return; // another session: nothing leaves with its cookie
    let answer: { status: number; retryAfter?: number; code?: string };
    try {
      answer = await api.putPosition(waiting.body);
    } catch {
      result.retryAfterMs = DEFAULT_RETRY_MS; // offline: the queue stays as it is
      return;
    }
    if (answer.status >= 200 && answer.status < 300) {
      await store.dropPosition(profileId, waiting.key);
      result.sent += 1;
    } else if (isLost(answer.status, answer.code)) {
      await store.wipe(profileId);
      result.wiped = true;
      return;
    } else if (retryable(answer.status) || answer.status === 403) {
      result.retryAfterMs = delay(answer.retryAfter); // kept
      return;
    } else {
      await store.dropPosition(profileId, waiting.key); // a refusal that will not change
    }
  }
}

/** The removals since the token, or a resynchronisation. Never evicts on a failure it cannot read. */
/** Resolves true when the token now stands for the present (nothing can leave the view unseen). */
export async function syncSince({ store, profileId, api }: Options, result: Pick<SyncResult, 'evicted' | 'wiped'>): Promise<boolean> {
  const lost = async (): Promise<false> => {
    await store.wipe(profileId);
    result.wiped = true;
    return false;
  };
  try {
    const token = await store.since(profileId);
    const answer = await api.works(token);
    if (answer.status === 401 || answer.status === 403) return await lost();
    if (answer.status === 200 && token !== null && answer.since) {
      result.evicted.push(...(await store.evictWorks(profileId, answer.removed ?? [])));
      // an Edition that left her view while its Work stays visible through another one: its copy goes too
      for (const work of await store.evictEditions(profileId, answer.removedEditions ?? [])) {
        if (!result.evicted.includes(work)) result.evicted.push(work);
      }
      await store.setSince(profileId, answer.since);
      return true;
    }
    // 410 (too old, other scope) and 400 (a token this instance does not know) are a resynchronisation
    if (answer.status !== 410 && answer.status !== 400 && !(answer.status === 200 && token === null)) return false;
    // take a token first, so that nothing that changes while the copies are asked again is missed
    const fresh = token === null && answer.since ? answer : await api.works(null);
    if (fresh.status === 401 || fresh.status === 403) return await lost();
    if (fresh.status !== 200 || !fresh.since) return false;
    let complete = true;
    for (const copy of await store.copies(profileId)) {
      const asked = await api.publication(copy.carrierType, copy.carrierId);
      if (asked.status === 401 || asked.status === 403) return await lost();
      if (asked.status === 404) {
        await store.evictCarrier(profileId, copy.carrierType, copy.carrierId);
        if (!result.evicted.includes(copy.workId)) result.evicted.push(copy.workId);
      } else if (asked.status !== 200) {
        complete = false; // not asked for real (busy, error): the token must not move past it
      }
    }
    if (!complete) return false;
    await store.setSince(profileId, fresh.since);
    return true;
  } catch {
    return false; // offline: nothing is evicted and nothing is wiped; the next reconnection asks again
  }
}

export async function syncOffline(options: Options): Promise<SyncResult> {
  const result: SyncResult = { sent: 0, evicted: [], wiped: false, retryAfterMs: null };
  // a stale tab, or someone else's session, never sends nor evicts for the profile on the browser
  if ((await options.store.activeProfile()) !== options.profileId) return result;
  await flush(options, result);
  if (!result.wiped && (await options.store.activeProfile()) === options.profileId && (!options.sessionOk || (await options.sessionOk()))) {
    const renewed = await syncSince(options, result);
    // `?since=` could not be read (unreachable while the browser says it is online, busy): no `online`
    // event will come, so the sync comes back by itself while a copy could still be one to evict
    if (!renewed && !result.wiped && result.retryAfterMs === null && (await options.store.copies(options.profileId)).length > 0) {
      result.retryAfterMs = DEFAULT_RETRY_MS;
    }
  }
  return result;
}

/** The `put` the reader's position sender uses for a copy: it sends, and queues what cannot go. */
export function createQueueingPut(options: {
  store: OfflineStore;
  profileId: string;
  put: (body: PositionIn) => Promise<{ status: number; retryAfter?: number; code?: string }>;
  sessionOk?: () => Promise<boolean>;
}): (body: PositionIn) => Promise<void> {
  return async (body) => {
    try {
      // another session than the profile's: the position waits, it never goes with someone else's cookie
      if (options.sessionOk && !(await options.sessionOk())) throw new Error('another session');
      const answer = await options.put(body);
      if (retryable(answer.status) || answer.status === 403) await options.store.queuePosition(options.profileId, body);
    } catch {
      await options.store.queuePosition(options.profileId, body);
    }
  };
}
