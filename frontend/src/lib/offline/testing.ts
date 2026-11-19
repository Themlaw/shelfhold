// Test support for the offline copies (M5 L16b): the browser stores, in memory. Never imported by
// the app.
import type { Kv, OfflineEnv } from './env.ts';

class MemoryCache {
  entries = new Map<string, Response>();
  async put(request: Request | string, response: Response) {
    this.entries.set(typeof request === 'string' ? request : request.url, response.clone());
  }
  async match(request: Request | string) {
    return this.entries.get(typeof request === 'string' ? request : request.url)?.clone();
  }
  async keys() {
    return [...this.entries.keys()].map((url) => new Request(url));
  }
}

class MemoryCaches {
  stores = new Map<string, MemoryCache>();
  async open(name: string) {
    if (!this.stores.has(name)) this.stores.set(name, new MemoryCache());
    return this.stores.get(name)!;
  }
  async has(name: string) {
    return this.stores.has(name);
  }
  async keys() {
    return [...this.stores.keys()];
  }
  async delete(name: string) {
    return this.stores.delete(name);
  }
}

export interface MemoryEnv extends OfflineEnv {
  memory: { caches: MemoryCaches; kvs: Map<string, Map<string, unknown>> };
  advance: (ms: number) => void;
}

export function memoryEnv(start = Date.parse('2026-11-19T10:00:00Z')): MemoryEnv {
  const caches = new MemoryCaches();
  const kvs = new Map<string, Map<string, unknown>>();
  let clock = start;
  const open = (name: string): Kv => {
    const data = kvs.get(name) ?? new Map<string, unknown>();
    kvs.set(name, data);
    return {
      get: async (key) => (data.has(key) ? structuredClone(data.get(key)) : undefined),
      set: async (key, value) => void data.set(key, structuredClone(value)),
      delete: async (key) => void data.delete(key),
      entries: async () => [...data.entries()].map(([k, v]) => [k, structuredClone(v)] as [string, unknown])
    };
  };
  return {
    caches: caches as unknown as CacheStorage,
    openKv: open,
    deleteKv: async (name) => void kvs.delete(name),
    now: () => clock,
    memory: { caches, kvs },
    advance: (ms) => void (clock += ms)
  };
}
