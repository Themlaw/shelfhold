// « Lire hors ligne » (M5 L16b, EF-LEC-012): a visible publication is kept for the signed-in
// profile; an extract never is (D-P1); a failed download leaves nothing behind.
import { describe, expect, it, vi } from 'vitest';
import type { PublicationOut } from '#lib/api/types.gen.ts';
import { keepCopy, OfflineKeepError } from './keep.ts';
import { createOfflineStore, serveCopyEntry } from './store.ts';
import { memoryEnv } from './testing.ts';
import type { OfflineApi } from './sync.ts';

const ALICE = '01JALICE00000000000000000A';
const CARRIER = '01JCARRIER000000000000000C';
const WORK = '01JWORK00000000000000000W';

const out = (over: Partial<PublicationOut> = {}): PublicationOut => ({
  carrier_type: 'content', carrier_id: CARRIER, work_id: WORK, scope: 'full', kind: 'pages', title: 'Tale', number: '1', language: 'en',
  reading_direction: 'rtl', expires_at: '2026-11-19T10:10:00Z',
  pages: [
    { index: 1, href: '/content/TICKET/p1.png', media_type: 'image/png' },
    { index: 2, href: '/content/TICKET/dir%20x/p2.png', media_type: 'image/png' }
  ],
  spine: [], package: null, ...over
});

function bench(publication: PublicationOut, entry: (url: string) => Promise<Response>) {
  const env = memoryEnv();
  const store = createOfflineStore(env);
  const api: OfflineApi & { entry: (url: string) => Promise<Response> } = {
    works: vi.fn(async () => ({ status: 200, since: 'tok', removed: [] })),
    publication: vi.fn(async () => ({ status: 200, publication })),
    putPosition: vi.fn(async () => ({ status: 200 })),
    entry
  };
  return { env, store, api };
}

const ok = async (url: string) => new Response(`bytes of ${url}`, { headers: { 'content-type': 'image/png', 'content-security-policy': 'sandbox' } });

describe('keeping a copy', () => {
  // req: EF-LEC-012
  it('keeps every entry under the profile, rewrites the links to the copy, and takes the token first', async () => {
    const { env, store, api } = bench(out(), ok);
    await store.activate(ALICE);
    const kept = await keepCopy({ store, profileId: ALICE, api, carrierType: 'unit', carrierId: '01JUNIT00000000000000000U' });
    expect(kept.workId).toBe(WORK);
    expect(await store.since(ALICE)).toBe('tok'); // a token older than the copy: a later change is a removal
    const [copy] = await store.copies(ALICE);
    expect(copy!.carrierId).toBe(CARRIER);
    const hrefs = copy!.publication.pages.map((p) => p.href);
    expect(hrefs.every((h) => h.startsWith('/content/offline.content.') && !h.includes('TICKET'))).toBe(true);
    for (const href of hrefs) expect((await serveCopyEntry(env, new URL(`https://x.example${href}`))).status).toBe(200);
    expect(new Date(copy!.publication.expires_at).getTime()).toBeGreaterThan(Date.parse('2030-01-01'));
  });

  // req: EF-LEC-012, EF-LEC-014
  it('refuses an extract: no extract is ever kept offline (D-P1)', async () => {
    const { store, api } = bench(out({ scope: 'excerpt' }), ok);
    await store.activate(ALICE);
    await expect(keepCopy({ store, profileId: ALICE, api, carrierType: 'unit', carrierId: 'U' })).rejects.toMatchObject({ reason: 'excerpt' });
    expect(await store.copies(ALICE)).toEqual([]);
  });

  // req: EF-LEC-012, EF-USR-006
  it('a carrier the profile may not see (404) is "not readable", with nothing kept', async () => {
    const { store, api } = bench(out(), ok);
    api.publication = vi.fn(async () => ({ status: 404 }));
    await store.activate(ALICE);
    await expect(keepCopy({ store, profileId: ALICE, api, carrierType: 'unit', carrierId: 'U' })).rejects.toBeInstanceOf(OfflineKeepError);
    expect(await store.copies(ALICE)).toEqual([]);
  });

  // req: EF-LEC-012
  it('a download that fails midway leaves no half copy', async () => {
    let n = 0;
    const { env, store, api } = bench(out(), async (url) => (++n === 2 ? new Response('no', { status: 500 }) : ok(url)));
    await store.activate(ALICE);
    await expect(keepCopy({ store, profileId: ALICE, api, carrierType: 'unit', carrierId: 'U' })).rejects.toMatchObject({ reason: 'download_failed' });
    expect(await store.copies(ALICE)).toEqual([]);
    expect(await env.caches.keys()).toEqual([]);
  });

  // req: EF-LEC-012, ENF-SEC-010
  it('a link of the publication that leaves the content route is not fetched', async () => {
    const entry = vi.fn(ok);
    const bad = out({ pages: [{ index: 1, href: 'https://elsewhere.example/p1.png', media_type: 'image/png' }] });
    const { store, api } = bench(bad, entry);
    await store.activate(ALICE);
    await expect(keepCopy({ store, profileId: ALICE, api, carrierType: 'unit', carrierId: 'U' })).rejects.toMatchObject({ reason: 'download_failed' });
    expect(entry).not.toHaveBeenCalled();
  });
});
