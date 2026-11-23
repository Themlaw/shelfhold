// What the offline copies need from the browser, behind a seam (M5 L16b): Cache Storage for the
// bytes of a book, a small key-value store (IndexedDB) for the rest. One cache and one store *per
// profile*: wiping a profile is deleting two names, and nothing of one profile is ever in a name
// another reads.

export interface Kv {
  get(key: string): Promise<unknown>;
  set(key: string, value: unknown): Promise<void>;
  delete(key: string): Promise<void>;
  entries(): Promise<[string, unknown][]>;
}

export interface OfflineEnv {
  caches: CacheStorage;
  openKv(name: string): Kv;
  deleteKv(name: string): Promise<void>;
  /** The names of the stores that exist, when the browser can list them (IndexedDB `databases()`):
   * a store of a profile nobody remembers is found by its name and deleted at open. Absent: such a
   * store stays until its profile is known again. */
  listKv?(): Promise<string[]>;
  now(): number;
  /** Runs `work` while no other tab does the same (Web Locks); absent: only this page's own order. */
  lock?<T>(name: string, work: () => Promise<T>): Promise<T>;
}

export const COPY_CACHE_PREFIX = 'shelfhold-copy-';
const KV_PREFIX = 'shelfhold-offline-';
/** Who is signed in on this browser, and every profile that holds a copy (to wipe them all). */
export const ACTIVE_KV = `${KV_PREFIX}active`;

export const copyCacheName = (profileId: string): string => `${COPY_CACHE_PREFIX}${profileId}`;
export const profileKvName = (profileId: string): string => `${KV_PREFIX}p-${profileId}`;

/** The profile a store belongs to when its name is one of ours; null for any other name (the
 * active store itself, a store of another feature). */
export const profileOfKvName = (name: string): string | null => {
  const within = name.startsWith(`${KV_PREFIX}p-`) ? name.slice(`${KV_PREFIX}p-`.length) : '';
  return within === '' ? null : within;
};

const ULID = /^[0-9A-Za-z]{1,40}$/;
export const isCarrierId = (value: string): boolean => ULID.test(value);

/** The path under which the bytes of an entry of a copy are answered: a name no server ticket has
 * (a ticket is `<body>.<tag>`, its body starting with `WzEs`; `offline.` never does), and `COPY_PATH`
 * is the only place the worker reads it: a request for it is a copy or a 404, never the network. */
export function copyUrl(carrierType: string, carrierId: string, entry: string): string {
  return `/content/offline.${carrierType}.${carrierId}/${entry.split('/').map(encodeURIComponent).join('/')}`;
}

export const COPY_PATH = /^\/content\/offline\.(content|export_item)\.([0-9A-Za-z]{1,40})\/(.+)$/;
