// What a change of the offline copies tells (M6 L20, EF-LEC-012): the tab that keeps, evicts or
// wipes a copy says so; the others hear it over a BroadcastChannel, and the tab itself directly
// (a channel never delivers to the context that posted). `/hors-ligne` reads its list again — an
// eviction by `?since=` shows up without a reload, in the tab that synced and in the others.
import type { OfflineStore } from './store.ts';

/** The name of the message, and of the channel between the tabs of one browser. */
export const OFFLINE_CHANGED = 'shelfhold-offline-changed';

/** The part of a BroadcastChannel the events need, behind a seam (the tests wire two tabs by hand). */
export interface OfflineChannel {
  /** Says it to the other tabs of this browser. */
  postMessage(message: string): void;
  /** Hears what they say; wired once, by `createOfflineEvents`. */
  hear(listener: (message: string) => void): void;
  close(): void;
}

export interface OfflineEvents {
  /** The copies changed: say so, here and in the other tabs. */
  changed(): void;
  /** Hear every change; what this returns stops hearing. */
  onChanged(listener: () => void): () => void;
}

export function createOfflineEvents(channel: OfflineChannel | null): OfflineEvents {
  const listeners = new Set<() => void>();
  if (channel) {
    channel.hear((message) => {
      if (message !== OFFLINE_CHANGED) return;
      for (const listener of [...listeners]) listener(); // a snapshot: a listener may stop hearing
    });
  }
  return {
    changed: () => {
      for (const listener of [...listeners]) listener();
      channel?.postMessage(OFFLINE_CHANGED);
    },
    onChanged: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    }
  };
}

/** Every change of the copies — kept, evicted, wiped, the profile changed — tells the screens that
 * show them, in this tab and in the others. A change that fails still tells: the list is stale
 * either way. Reads say nothing. */
export function notifying(store: OfflineStore, events: { changed(): void }): OfflineStore {
  const said = <T>(work: () => Promise<T>): Promise<T> => work().finally(() => events.changed());
  return {
    ...store,
    activate: (profileId) => said(() => store.activate(profileId)),
    keep: (profileId, meta, entries) => said(() => store.keep(profileId, meta, entries)),
    evictCarrier: (profileId, type, id) => said(() => store.evictCarrier(profileId, type, id)),
    evictWorks: (profileId, workIds) => said(() => store.evictWorks(profileId, workIds)),
    evictEditions: (profileId, editionIds) => said(() => store.evictEditions(profileId, editionIds)),
    wipe: (profileId) => said(() => store.wipe(profileId)),
    wipeAll: () => said(() => store.wipeAll())
  };
}
