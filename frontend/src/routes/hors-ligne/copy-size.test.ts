// The size shown for a copy kept on this device (EF-LEC-012).
import { describe, expect, it } from 'vitest';
import { copyCacheName } from '#lib/offline/env.ts';
import { memoryEnv } from '#lib/offline/testing.ts';
import { copySize } from './copy-size.ts';

const ALICE = '01JALICE00000000000000000A';

describe('the size of a kept copy', () => {
  // req: EF-LEC-012
  it('adds the entries of this copy only, from their headers or their bytes', async () => {
    const env = memoryEnv();
    const cache = await env.caches.open(copyCacheName(ALICE));
    await cache.put('/content/offline.content.C1/a.jpg', new Response('x', { headers: { 'content-length': '1000' } }));
    await cache.put('/content/offline.content.C1/b.jpg', new Response('12345'));
    await cache.put('/content/offline.content.C2/a.jpg', new Response('other copy', { headers: { 'content-length': '9999' } }));
    expect(await copySize(env.caches, ALICE, { carrierType: 'content', carrierId: 'C1' })).toBe(1005);
  });

  // req: EF-LEC-012
  it('says nothing rather than a wrong figure when it cannot read', async () => {
    const env = memoryEnv();
    expect(await copySize(undefined, ALICE, { carrierType: 'content', carrierId: 'C1' })).toBeNull();
    expect(await copySize(env.caches, ALICE, { carrierType: 'content', carrierId: 'C1' })).toBeNull();
  });
});
