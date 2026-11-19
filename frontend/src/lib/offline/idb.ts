// The browser's stores behind `OfflineEnv` (M5 L16b): IndexedDB as a key-value store, one database
// per name. Thin on purpose: the rules are in `store.ts`, which the tests run on memory.
import type { Kv, OfflineEnv } from './env.ts';

function wait<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('indexeddb'));
  });
}

function open(factory: IDBFactory, name: string): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = factory.open(name, 1);
    request.onupgradeneeded = () => request.result.createObjectStore('kv');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('indexeddb'));
  });
}

async function run<T>(factory: IDBFactory, name: string, mode: IDBTransactionMode, work: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await open(factory, name);
  try {
    return await wait(work(db.transaction('kv', mode).objectStore('kv')));
  } finally {
    db.close();
  }
}

export function idbEnv(scope: { indexedDB: IDBFactory; caches: CacheStorage }): OfflineEnv {
  const factory = scope.indexedDB;
  return {
    caches: scope.caches,
    now: () => Date.now(),
    deleteKv: (name) =>
      new Promise((resolve, reject) => {
        const request = factory.deleteDatabase(name);
        request.onsuccess = () => resolve();
        request.onerror = () => reject(request.error ?? new Error('indexeddb'));
        request.onblocked = () => resolve(); // closed as soon as the last transaction ends
      }),
    openKv(name): Kv {
      return {
        get: (key) => run(factory, name, 'readonly', (s) => s.get(key)),
        set: async (key, value) => void (await run(factory, name, 'readwrite', (s) => s.put(value, key))),
        delete: async (key) => void (await run(factory, name, 'readwrite', (s) => s.delete(key))),
        entries: async () => {
          const keys = (await run(factory, name, 'readonly', (s) => s.getAllKeys())) as string[];
          const values = (await run(factory, name, 'readonly', (s) => s.getAll())) as unknown[];
          return keys.map((key, i) => [key, values[i]] as [string, unknown]);
        }
      };
    }
  };
}
