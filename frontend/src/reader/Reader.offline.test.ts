// The reader with no network (M5 L16b, EF-LEC-012, EF-LEC-007): it opens the copy the signed-in
// profile kept on this device, says so, and what it reads waits in the profile's queue with the time
// it was read at. Nothing is opened when there is no copy, or when the book is an extract.
import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import type { PublicationOut } from '#lib/api/types.gen.ts';
import { createOfflineStore, type OfflineStore } from '#lib/offline/store.ts';
import { memoryEnv } from '#lib/offline/testing.ts';
import Page from './Reader.svelte';

const holder = vi.hoisted(() => ({ store: null as unknown as OfflineStore }));
vi.mock('#lib/offline/runtime.ts', () => ({ offlineStore: () => holder.store }));

const BASE = 'http://shelfhold.test';
const server = setupServer();
const ALICE = '01JALICE00000000000000000A';

const kept: PublicationOut = {
  carrier_type: 'content', carrier_id: 'C1', work_id: 'W1', scope: 'full', kind: 'pages', title: 'Une série', number: '3', language: 'fr',
  reading_direction: 'ltr', expires_at: '2036-11-18T12:10:00Z',
  pages: [1, 2, 3].map((n) => ({ index: n, href: `/content/offline.content.C1/p${n}.jpg`, media_type: 'image/jpeg' })),
  spine: [], package: null
};

let puts = 0;
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
  const patched = globalThis.fetch;
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => patched(typeof input === 'string' && input.startsWith('/') ? `${BASE}${input}` : input, init));
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: () => `blob:t/${Math.random()}`, revokeObjectURL: () => undefined }));
});
beforeEach(async () => {
  document.cookie = 'csrf=tok123';
  puts = 0;
  holder.store = createOfflineStore(memoryEnv());
  server.use(
    http.get(`${BASE}/api/reader/unit/U1/publication`, () => HttpResponse.error()),
    http.put(`${BASE}/api/reader/positions`, () => { puts += 1; return HttpResponse.error(); }),
    http.get(`${BASE}/content/offline.content.C1/:name`, () => new HttpResponse(new Uint8Array(16), { headers: { 'Content-Type': 'image/jpeg' } }))
  );
});
afterEach(() => { cleanup(); server.resetHandlers(); });
afterAll(() => { server.close(); vi.unstubAllGlobals(); });

async function keepIt(publication = kept) {
  await holder.store.activate(ALICE);
  await holder.store.keep(ALICE, { carrierType: 'content', carrierId: 'C1', workId: 'W1', title: 'Une série', via: 'U1', publication }, []);
}
const props = { carrierType: 'unit', carrierId: 'U1', scope: null, guard: async () => 'ok' as const };

describe('the reader with no network', () => {
  // req: EF-LEC-012
  it('test_pwa_offline_cache: opens the kept copy and says it is one', async () => {
    await keepIt();
    render(Page, { props });
    expect(await screen.findByText('Copie hors ligne')).toBeInTheDocument();
    expect(await screen.findByAltText('Page 1 sur 3')).toBeInTheDocument();
  });

  // req: EF-LEC-012, ENF-SEC-010
  it('no copy for this profile: says so, opens nothing', async () => {
    render(Page, { props });
    expect(await screen.findByRole('alert')).toHaveTextContent('n’a pas de copie hors ligne');
    expect(screen.queryByAltText('Page 1 sur 3')).toBeNull();
  });

  // req: EF-LEC-012, ENF-SEC-010
  it('another profile on the browser has no copy to open: the store holds nothing of the first', async () => {
    await keepIt();
    await holder.store.activate('01JBOB0000000000000000000B');
    render(Page, { props });
    expect(await screen.findByRole('alert')).toHaveTextContent('n’a pas de copie hors ligne');
  });

  // req: EF-LEC-012, ENF-SEC-010
  it('a session other than the one the copy was kept under opens nothing', async () => {
    await keepIt();
    await holder.store.bindSession('tok-of-the-first-session');
    document.cookie = 'csrf=tok-of-another-profile';
    render(Page, { props });
    expect(await screen.findByRole('alert')).toHaveTextContent('n’a pas de copie hors ligne');
    expect(screen.queryByAltText('Page 1 sur 3')).toBeNull();
  });

  // req: EF-LEC-012
  it('an extract is never read from a copy', async () => {
    await keepIt();
    render(Page, { props: { ...props, scope: 'excerpt' as const } });
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByAltText('Page 1 sur 3')).toBeNull();
  });

  // req: EF-LEC-007
  it('offline positions merged on reconnect: a page turned offline waits in the queue with its client_ts', async () => {
    await keepIt();
    const user = userEvent.setup();
    render(Page, { props });
    await screen.findByAltText('Page 1 sur 3');
    await user.keyboard('{ArrowRight}');
    await screen.findByAltText('Page 2 sur 3');
    cleanup(); // leaving sends what waits; the network is down, so it is queued
    await waitFor(async () => expect(await holder.store.queued(ALICE)).toHaveLength(1), { timeout: 3000 });
    const [waiting] = await holder.store.queued(ALICE);
    expect(waiting!.body).toMatchObject({ carrier_type: 'content', carrier_id: 'C1', scope: 'full' });
    expect(Number.isFinite(Date.parse(waiting!.body.client_ts))).toBe(true);
    expect(puts).toBeGreaterThan(0);
  });
});
