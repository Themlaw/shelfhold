// The size of one copy kept on this device: the bytes of every entry the copy holds in the cache of
// the profile. The size is read from the headers the entry was stored with, so that no file is
// loaded into memory to be measured; an entry that carries none is measured as a blob. Nothing is
// written, and a browser with no Cache Storage (or one that refuses it) gives `null`: the screen
// then says the size is unknown rather than a wrong figure.
import { copyCacheName } from '#lib/offline/env.ts';

export async function copySize(
  storage: CacheStorage | undefined,
  profileId: string,
  copy: { carrierType: string; carrierId: string }
): Promise<number | null> {
  if (!storage) return null;
  try {
    if (!(await storage.has(copyCacheName(profileId)))) return null;
    const cache = await storage.open(copyCacheName(profileId));
    const prefix = `/content/offline.${copy.carrierType}.${copy.carrierId}/`;
    let total = 0;
    let found = false;
    for (const request of await cache.keys()) {
      if (!new URL(request.url).pathname.startsWith(prefix)) continue;
      const response = await cache.match(request);
      if (!response) continue;
      const declared = Number(response.headers.get('content-length'));
      total += Number.isFinite(declared) && declared > 0 ? declared : (await response.blob()).size;
      found = true;
    }
    return found ? total : null;
  } catch {
    return null;
  }
}
