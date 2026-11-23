// The offline copies in the browser (M5 L16b): the store on IndexedDB and Cache Storage, the calls
// of the API behind the seam of `sync.ts`, and the moments the sync runs (at start, when the
// network comes back, and after the delay a busy server asked for). M6 L20: every change of the
// copies is said to the screens and the other tabs (`events.ts`).
import { listWorks, readerPublication, readerPutPosition } from '#lib/api/index.ts';
import { csrfToken } from '#lib/passkeys.ts';
import { OFFLINE_CHANGED, createOfflineEvents, notifying, type OfflineChannel, type OfflineEvents } from './events.ts';
import { idbEnv } from './idb.ts';
import type { KeepApi } from './keep.ts';
import { createOfflineStore, type OfflineStore } from './store.ts';
import { syncOffline } from './sync.ts';

/** The channel between the tabs of this browser, when the browser offers one; nothing otherwise (a
 * channel never delivers to the context that posted, which is why the events also tell locally). */
function channelOf(name: string): OfflineChannel | null {
  if (typeof BroadcastChannel === 'undefined') return null;
  try {
    const real = new BroadcastChannel(name);
    return {
      postMessage: (message) => real.postMessage(message),
      hear: (listener) => (real.onmessage = (event) => listener(event.data)),
      close: () => real.close()
    };
  } catch {
    return null; // no channel in this context: the tab that changes is the one that hears
  }
}

let events: OfflineEvents | null = null;

/** The one place the changes of the copies are said (M6 L20). */
export function offlineEvents(): OfflineEvents {
  events ??= createOfflineEvents(channelOf(OFFLINE_CHANGED));
  return events;
}

/** What the screens hear: a copy kept, evicted or wiped, in this tab or in another one. */
export function onOfflineChanged(listener: () => void): () => void {
  return offlineEvents().onChanged(listener);
}

let store: OfflineStore | null = null;

export function offlineStore(): OfflineStore {
  // every change goes through `notifying`: the screens that show the copies read again
  store ??= notifying(createOfflineStore(idbEnv(window)), offlineEvents());
  return store;
}

/** The client returns a failed fetch as an `error` with no `response`: for the sync that is the network being down. */
function answered<T extends { response?: Response; error?: unknown }>(result: T): T & { response: Response } {
  if (result.response === undefined) throw result.error instanceof Error ? result.error : new TypeError('network');
  return result as T & { response: Response };
}

export function offlineApi(): KeepApi {
  return {
    async works(since) {
      const { data, response } = answered(await listWorks({ query: { limit: 1, ...(since ? { since } : {}) } }));
      return { status: response.status, since: data?.since, removed: data?.removed };
    },
    async publication(carrierType, carrierId) {
      const { data, response } = answered(await readerPublication({ path: { carrier_type: carrierType, carrier_id: carrierId } }));
      return { status: response.status, ...(data ? { publication: data } : {}) };
    },
    async putPosition(body) {
      const { response, error } = answered(await readerPutPosition({ body, headers: { 'X-CSRF-Token': csrfToken() }, keepalive: true }));
      const code = (error as { code?: unknown } | undefined)?.code;
      const retry = Number(response.headers.get('Retry-After'));
      return { status: response.status, ...(typeof code === 'string' ? { code } : {}), ...(Number.isFinite(retry) && retry > 0 ? { retryAfter: retry } : {}) };
    },
    entry: (url) => fetch(url, { credentials: 'same-origin' })
  };
}

/** Sign-out and a lost session: nothing of the profile stays on the browser. Never fails. */
export async function wipeOfflineCopies(): Promise<void> {
  try {
    await offlineStore().wipeAll();
  } catch {
    /* no storage: there is nothing kept */
  }
}

/** Whether the session of the browser is the one the profile on it was bound to. Nobody to compare
 * (no cookie, nothing recorded): not another session, so true. */
export async function sessionIsBound(store: OfflineStore = offlineStore()): Promise<boolean> {
  const token = csrfToken();
  return !token || (await store.sessionMatches(token)) !== false;
}

/** The profile on this browser is tied to the session that is signed in (a hash of its anti-forgery token). */
async function bindToSession(): Promise<void> {
  const token = csrfToken();
  if (token) await offlineStore().bindSession(token);
}

/** The session in this browser is not the one the copies were kept under, and the server did not say
 * who signed in (`/me` failed: busy, throttled): another profile may have signed in, so nothing of the
 * previous one stays. A same profile with a new session is bound again. Never fails; a browser that
 * cannot tell (no cookie, no record) is left as it is. */
export async function guardOfflineSession(profileId: string | null): Promise<void> {
  try {
    const token = csrfToken();
    const store = offlineStore();
    const active = await store.activeProfile();
    if (!token || !active) return;
    const same = await store.sessionMatches(token);
    if (same === true) return;
    // the server named someone: `activate` (startOfflineSync) wipes any other profile's copies and binds the
    // session, and a wipe here could land after it. Only an unnamed session is judged here.
    if (profileId !== null) {
      if (profileId === active) await store.bindSession(token);
      return;
    }
    if (same === false) await wipeOfflineCopies();
  } catch {
    /* no storage: there is nothing kept */
  }
}

/** The profile on this browser is this one: another one's copies are gone, then a first sync, and
 * again whenever the network comes back or a busy server said to. Returns the way to stop. */
export function startOfflineSync(profileId: string, options: { api?: KeepApi; target?: Pick<Window, 'addEventListener' | 'removeEventListener'> } = {}): () => void {
  const api = options.api ?? offlineApi();
  const target = options.target ?? window;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;
  const run = async () => {
    if (stopped) return;
    if (timer !== null) clearTimeout(timer);
    timer = null;
    try {
      const result = await syncOffline({ store: offlineStore(), profileId, api, sessionOk: () => sessionIsBound() });
      if (result.retryAfterMs !== null && !stopped) timer = setTimeout(() => void run(), result.retryAfterMs);
    } catch {
      /* the next reconnection tries again */
    }
  };
  void offlineStore()
    .activate(profileId)
    .then(() => bindToSession())
    .then(run, () => undefined);
  const onOnline = () => void run();
  target.addEventListener('online', onOnline);
  return () => {
    stopped = true;
    if (timer !== null) clearTimeout(timer);
    target.removeEventListener('online', onOnline);
  };
}
