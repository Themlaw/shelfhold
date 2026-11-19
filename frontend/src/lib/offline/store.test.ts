// The offline copies of the PWA (M5 L16b, EF-LEC-012 passe 12, ADR-0055 « Crédential », R66).
// A copy belongs to one profile: never readable by another on the same browser, wiped at sign-out,
// wiped when the profile changes, and a book is never kept in a cache shared by profiles.
import { describe, expect, it } from 'vitest';
import type { PublicationOut } from '#lib/api/types.gen.ts';
import { copyCacheName, copyUrl } from './env.ts';
import { createOfflineStore, serveCopyEntry } from './store.ts';
import { memoryEnv } from './testing.ts';

const ALICE = '01JALICE00000000000000000A';
const BOB = '01JBOB0000000000000000000B';
const CARRIER = '01JCARRIER000000000000000C';
const WORK = '01JWORK00000000000000000W';

function publication(carrierId = CARRIER, workId = WORK): PublicationOut {
  return {
    carrier_type: 'content',
    carrier_id: carrierId,
    work_id: workId,
    scope: 'full',
    kind: 'pages',
    title: 'Tale of Pages',
    number: '1',
    language: 'en',
    reading_direction: 'ltr',
    expires_at: '2026-11-19T10:10:00Z',
    pages: [{ index: 1, href: '/content/TICKET/p1.png', media_type: 'image/png' }],
    spine: [],
    package: null
  };
}

const headers = { 'content-type': 'image/png', 'content-security-policy': 'sandbox', 'cache-control': 'private' };

async function keepFor(store: ReturnType<typeof createOfflineStore>, profile: string, carrier = CARRIER, work = WORK) {
  await store.activate(profile);
  await store.keep(profile, { carrierType: 'content', carrierId: carrier, workId: work, title: 'Tale of Pages', publication: publication(carrier, work) }, [
    { url: copyUrl('content', carrier, 'p1.png'), response: new Response('PNG-BYTES', { headers }) }
  ]);
}

describe('offline copies are kept per profile', () => {
  // req: EF-LEC-012
  it('offline copies are not shared between profiles', async () => {
    const env = memoryEnv();
    const store = createOfflineStore(env);
    await keepFor(store, ALICE);
    expect(await store.copies(ALICE)).toHaveLength(1);

    await store.activate(BOB); // the same browser, someone else signs in
    expect(await store.copies(ALICE)).toEqual([]); // hers are gone, not hidden
    expect(await store.copies(BOB)).toEqual([]);
    expect(await env.caches.has(copyCacheName(ALICE))).toBe(false);
    expect([...env.memory.kvs.keys()].filter((name) => name.includes(ALICE))).toEqual([]);
    // the bytes are not served to the new profile either
    const served = await serveCopyEntry(env, new URL(`https://x.example${copyUrl('content', CARRIER, 'p1.png')}`));
    expect(served.status).toBe(404);
  });

  // req: EF-LEC-012, ENF-SEC-010
  it('a copy is never kept for a profile that is not the active one', async () => {
    const store = createOfflineStore(memoryEnv());
    await store.activate(ALICE);
    await expect(
      store.keep(BOB, { carrierType: 'content', carrierId: CARRIER, workId: WORK, title: 't', publication: publication() }, [])
    ).rejects.toThrow(/not the active profile/);
    expect(await store.copies(BOB)).toEqual([]);
  });

  // req: EF-LEC-012, ENF-SEC-010
  it('the bytes of a copy come back with the headers they were served with, for the active profile only', async () => {
    const env = memoryEnv();
    const store = createOfflineStore(env);
    await keepFor(store, ALICE);
    const url = new URL(`https://x.example${copyUrl('content', CARRIER, 'p1.png')}`);
    const served = await serveCopyEntry(env, url);
    expect(served.status).toBe(200);
    expect(await served.text()).toBe('PNG-BYTES');
    for (const [name, value] of Object.entries(headers)) expect(served.headers.get(name)).toBe(value);
    // nobody signed in on this browser: nothing is served
    await store.wipeAll();
    expect((await serveCopyEntry(env, url)).status).toBe(404);
  });

  // req: EF-LEC-012
  it('a carrier that is not a copy of the active profile is a plain 404, never the network', async () => {
    const env = memoryEnv();
    const store = createOfflineStore(env);
    await keepFor(store, ALICE);
    for (const path of [
      copyUrl('content', '01JOTHER000000000000000000', 'p1.png'),
      copyUrl('content', CARRIER, 'p9.png'),
      copyUrl('content', CARRIER, '../p1.png'),
      `/content/offline.content.${CARRIER}/%2e%2e/p1.png`
    ]) {
      expect((await serveCopyEntry(env, new URL(`https://x.example${path}`))).status).toBe(404);
    }
  });
});

describe('logout wipes the profile offline copies', () => {
  // req: EF-LEC-012, ENF-SEC-010
  it('logout wipes the profile offline copies', async () => {
    const env = memoryEnv();
    const store = createOfflineStore(env);
    await keepFor(store, ALICE);
    await store.queuePosition(ALICE, { carrier_type: 'content', carrier_id: CARRIER, client_ts: '2026-11-19T10:00:00Z', locator: { v: 1 } });
    await store.setSince(ALICE, 'tok');

    await store.wipeAll();

    expect(await store.activeProfile()).toBeNull();
    expect(await store.copies(ALICE)).toEqual([]);
    expect(await store.queued(ALICE)).toEqual([]);
    expect(await store.since(ALICE)).toBeNull();
    expect(await env.caches.keys()).toEqual([]);
    expect([...env.memory.kvs.keys()].filter((name) => name.includes(ALICE))).toEqual([]);
  });

  // req: EF-LEC-012
  it('wipes the copies of a profile that lost its access without touching the others', async () => {
    const env = memoryEnv();
    const store = createOfflineStore(env);
    await keepFor(store, ALICE);
    await store.wipe(ALICE);
    expect(await store.copies(ALICE)).toEqual([]);
    expect(await env.caches.has(copyCacheName(ALICE))).toBe(false);
  });
});

describe('what leaves the view is evicted', () => {
  // req: EF-LEC-012, EF-INT-001
  it('evicts the copies of the Works named, and only those', async () => {
    const env = memoryEnv();
    const store = createOfflineStore(env);
    await keepFor(store, ALICE, CARRIER, WORK);
    await store.keep(ALICE, { carrierType: 'content', carrierId: '01JSECOND00000000000000002', workId: '01JWORK2000000000000000002', title: 'Other', publication: publication('01JSECOND00000000000000002', '01JWORK2000000000000000002') }, [
      { url: copyUrl('content', '01JSECOND00000000000000002', 'p1.png'), response: new Response('B', { headers }) }
    ]);
    expect(await store.evictWorks(ALICE, [WORK, '01JNEVERKEPT000000000000N'])).toEqual([WORK]);
    expect((await store.copies(ALICE)).map((c) => c.workId)).toEqual(['01JWORK2000000000000000002']);
    const gone = await serveCopyEntry(env, new URL(`https://x.example${copyUrl('content', CARRIER, 'p1.png')}`));
    expect(gone.status).toBe(404);
  });
});
