// « Lire hors ligne » in the reader (M6 L20, EF-LEC-012, D-P7 (b)): every profile that opens a book
// may keep it on this device — the sheet of a series is the administrator's, the reader is
// everyone's. An extract never offers it (D-P1: it is never kept), and a browser with nobody
// signed in keeps nothing.
import { cleanup, render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import type { PublicationOut } from '#lib/api/types.gen.ts';
import type { KeepApi } from '#lib/offline/keep.ts';
import { createOfflineStore, type OfflineStore } from '#lib/offline/store.ts';
import { memoryEnv } from '#lib/offline/testing.ts';
import Page from './Reader.svelte';

const holder = vi.hoisted(() => ({ store: null as unknown as OfflineStore, api: null as unknown as KeepApi }));
vi.mock('#lib/offline/runtime.ts', () => ({ offlineStore: () => holder.store, offlineApi: () => holder.api }));

const BASE = 'http://shelfhold.test';
const server = setupServer();
// a member, a child: the reader is every profile's (the administrator's sheet is not)
const ALICE = '01JMEMBER0000000000000000M';
const KID = '01JCHILD00000000000000000C';

const online: PublicationOut = {
  carrier_type: 'content', carrier_id: 'C1', work_id: 'W1', series_id: 'W1', edition_id: 'E1', scope: 'full', kind: 'pages', title: 'Une série', number: '3', language: 'fr',
  reading_direction: 'ltr', expires_at: '2036-11-18T12:10:00Z',
  pages: [1, 2, 3].map((n) => ({ index: n, href: `/content/TICKET/p${n}.jpg`, media_type: 'image/jpeg' })),
  spine: [], package: null
};

function keepApi(over: Partial<KeepApi> = {}): KeepApi {
  return {
    works: async () => ({ status: 200, since: 'tok', removed: [] }),
    publication: async () => ({ status: 200, publication: online }),
    putPosition: async () => ({ status: 200 }),
    entry: async () => new Response('x', { headers: { 'content-type': 'image/jpeg' } }),
    ...over
  };
}

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
  const patched = globalThis.fetch;
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => patched(typeof input === 'string' && input.startsWith('/') ? `${BASE}${input}` : input, init));
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: () => `blob:t/${Math.random()}`, revokeObjectURL: () => undefined }));
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
  holder.store = createOfflineStore(memoryEnv());
  holder.api = keepApi();
  server.use(
    http.get(`${BASE}/api/reader/unit/U1/publication`, () => HttpResponse.json(online)),
    http.get(`${BASE}/api/reader/settings`, () => HttpResponse.json({ direction: null, comfort: {} })),
    http.put(`${BASE}/api/reader/positions`, () => HttpResponse.json({ conflict: null, position: null })),
    http.get(`${BASE}/content/TICKET/:name`, () => new HttpResponse(new Uint8Array(16), { headers: { 'Content-Type': 'image/jpeg' } }))
  );
});
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => {
  server.close();
  vi.unstubAllGlobals();
});

const props = { carrierType: 'unit', carrierId: 'U1', scope: null, guard: async () => 'ok' as const };

describe('the reader offers the offline copy', () => {
  // req: EF-LEC-012
  it('keeps the copy for the signed-in profile and says so', async () => {
    await holder.store.activate(ALICE);
    const user = userEvent.setup();
    render(Page, { props });
    await screen.findByAltText('Page 1 sur 3');
    await user.click(screen.getByRole('button', { name: 'Lire hors ligne' }));
    expect(await screen.findByText('Copie gardée sur cet appareil')).toBeInTheDocument();
    expect(await holder.store.copies(ALICE)).toHaveLength(1);
    const [copy] = await holder.store.copies(ALICE);
    expect(copy).toMatchObject({ carrierType: 'content', carrierId: 'C1', workId: 'W1', via: 'U1' });
  });

  // req: EF-LEC-012, EF-USR-006
  it('a child keeps her own copy, never another profile’s', async () => {
    await holder.store.activate(KID);
    const user = userEvent.setup();
    render(Page, { props });
    await screen.findByAltText('Page 1 sur 3');
    await user.click(screen.getByRole('button', { name: 'Lire hors ligne' }));
    expect(await screen.findByText('Copie gardée sur cet appareil')).toBeInTheDocument();
    expect((await holder.store.copies(KID)).map((copy) => copy.via)).toEqual(['U1']);
  });

  // req: EF-LEC-012, EF-LEC-014
  it('an extract offers no offline copy (D-P1: an extract is never kept)', async () => {
    server.use(
      http.get(`${BASE}/api/reader/unit/U1/publication`, () => HttpResponse.json({ ...online, scope: 'excerpt' }))
    );
    render(Page, { props: { ...props, scope: 'excerpt' as const } });
    await screen.findByAltText('Page 1 sur 3');
    expect(screen.getByText('Extrait')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Lire hors ligne' })).toBeNull();
  });

  // req: EF-LEC-012
  it('nobody signed in on this browser: asks to sign in again, keeps nothing', async () => {
    const user = userEvent.setup();
    render(Page, { props });
    await screen.findByAltText('Page 1 sur 3');
    await user.click(screen.getByRole('button', { name: 'Lire hors ligne' }));
    expect(await screen.findByText('Reconnecte-toi pour garder une copie.')).toBeInTheDocument();
    expect(await holder.store.copies(ALICE)).toEqual([]);
  });
});
