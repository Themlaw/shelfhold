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
  now(): number;
}

export const COPY_CACHE_PREFIX = 'shelfhold-copy-';
const KV_PREFIX = 'shelfhold-offline-';
/** Who is signed in on this browser, and every profile that holds a copy (to wipe them all). */
export const ACTIVE_KV = `${KV_PREFIX}active`;

export const copyCacheName = (profileId: string): string => `${COPY_CACHE_PREFIX}${profileId}`;
export const profileKvName = (profileId: string): string => `${KV_PREFIX}p-${profileId}`;

const ULID = /^[0-9A-Za-z]{1,40}$/;
export const isCarrierId = (value: string): boolean => ULID.test(value);

/** The path under which the bytes of an entry of a copy are answered: a name no server ticket has
 * (a ticket is `<body>.<tag>`, its body starting with `WzEs`; `offline.` never does), and `COPY_PATH`
 * is the only place the worker reads it: a request for it is a copy or a 404, never the network. */
export function copyUrl(carrierType: string, carrierId: string, entry: string): string {
  return `/content/offline.${carrierType}.${carrierId}/${entry.split('/').map(encodeURIComponent).join('/')}`;
}

export const COPY_PATH = /^\/content\/offline\.(content|export_item)\.([0-9A-Za-z]{1,40})\/(.+)$/;
