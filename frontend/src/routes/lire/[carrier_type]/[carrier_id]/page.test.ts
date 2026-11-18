import { render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const state = vi.hoisted(() => ({
  page: {
    params: { carrier_type: 'unit', carrier_id: 'U1' },
    url: new URL('http://shelfhold.test/lire/unit/U1')
  }
}));
vi.mock('$app/state', () => state);

const BASE = 'http://shelfhold.test';
const server = setupServer();
const realFetch = globalThis.fetch;

type Pub = Record<string, unknown>;
const publication = (over: Pub = {}): Pub => ({
  carrier_id: 'C1',
  carrier_type: 'content',
  expires_at: '2026-11-18T12:10:00Z',
  kind: 'pages',
  language: 'fr',
  number: '3',
  package: null,
  pages: [1, 2, 3].map((n) => ({ index: n - 1, href: `/content/TICKET/p${n}.jpg`, media_type: 'image/jpeg' })),
  reading_direction: 'ltr',
  scope: 'full',
  spine: [],
  title: 'Une série',
  ...over
});

let puts: Record<string, unknown>[] = [];
let asked: string[] = [];
let publicationQuery: string | null = null;

const serve = (pub: Pub) => [
  http.get(`${BASE}/api/reader/unit/U1/publication`, ({ request }) => {
    publicationQuery = new URL(request.url).search;
    return HttpResponse.json(pub);
  }),
  http.put(`${BASE}/api/reader/positions`, async ({ request }) => {
    puts.push((await request.json()) as Record<string, unknown>);
    return HttpResponse.json({ conflict: null, position: null });
  }),
  http.get(`${BASE}/content/TICKET/:name`, ({ params }) => {
    asked.push(String(params.name));
    return new HttpResponse(new Uint8Array(16), { headers: { 'Content-Type': 'image/jpeg' } });
  })
];

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
  // a relative link into /content, as the manifest gives it, lives on the instance
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) =>
    realFetch(typeof input === 'string' && input.startsWith('/') ? `${BASE}${input}` : input, init)
  );
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: () => `blob:t/${Math.random()}`, revokeObjectURL: () => undefined }));
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
  puts = [];
  asked = [];
  publicationQuery = null;
  state.page.params = { carrier_type: 'unit', carrier_id: 'U1' };
  state.page.url = new URL('http://shelfhold.test/lire/unit/U1');
});
afterEach(() => server.resetHandlers());
afterAll(() => {
  server.close();
  vi.unstubAllGlobals();
});

describe('the reader of images', () => {
  // req: EF-LEC-001
  it('opens the first page, turns it with the keyboard and tells the screen reader', async () => {
    server.use(...serve(publication()));
    const user = userEvent.setup();
    render(Page);
    expect(await screen.findByRole('heading', { level: 1, name: /Une série/ })).toBeInTheDocument();
    expect(await screen.findByAltText('Page 1 sur 3')).toBeInTheDocument();
    await user.keyboard('{ArrowRight}');
    expect(await screen.findByAltText('Page 2 sur 3')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Page 2 sur 3');
    await user.click(screen.getByRole('button', { name: 'Page précédente' }));
    expect(await screen.findByAltText('Page 1 sur 3')).toBeInTheDocument();
  });

  // req: EF-LEC-001
  it('turns the arrows around for a book read right to left', async () => {
    server.use(...serve(publication({ reading_direction: 'rtl' })));
    const user = userEvent.setup();
    render(Page);
    await screen.findByAltText('Page 1 sur 3');
    await user.keyboard('{ArrowLeft}');
    expect(await screen.findByAltText('Page 2 sur 3')).toBeInTheDocument();
  });

  // req: EF-LEC-007
  it('sends the position it reached, once, against the file the server chose', async () => {
    server.use(...serve(publication()));
    const user = userEvent.setup();
    render(Page);
    await screen.findByAltText('Page 1 sur 3');
    await user.keyboard('{ArrowRight}{ArrowRight}');
    await waitFor(() => expect(puts).toHaveLength(1), { timeout: 3000 });
    expect(puts[0]).toMatchObject({
      carrier_type: 'content',
      carrier_id: 'C1',
      scope: 'full',
      locator: { v: 1, type: 'page', index: 2, count: 3 }
    });
  });

  // req: EF-UX-006
  it('an extract says so, writes nothing and ends on a sentence, not a blank page', async () => {
    state.page.url = new URL('http://shelfhold.test/lire/unit/U1?scope=excerpt');
    server.use(...serve(publication({ scope: 'excerpt' })));
    const user = userEvent.setup();
    render(Page);
    await screen.findByAltText('Page 1 sur 3');
    expect(publicationQuery).toBe('?scope=excerpt');
    expect(screen.getByText('Extrait')).toBeInTheDocument();
    await user.keyboard('{ArrowRight}{ArrowRight}{ArrowRight}');
    expect(await screen.findByText('Fin de l’extrait')).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 1200));
    expect(puts).toEqual([]);
    expect(asked.every((name) => /^p[123]\.jpg$/.test(name))).toBe(true);
  });

  // req: EF-UX-006
  it('does not offer to read on in Kavita or anywhere else from an extract', async () => {
    server.use(...serve(publication({ scope: 'excerpt' })));
    render(Page);
    await screen.findByAltText('Page 1 sur 3');
    expect(screen.queryByRole('link', { name: /Kavita/ })).toBeNull();
  });

  // req: EF-LEC-002
  it('lays out a webtoon as a column', async () => {
    server.use(...serve(publication({ reading_direction: 'webtoon' })));
    const { container } = render(Page);
    await screen.findByAltText('Page 1 sur 3');
    expect(container.querySelector('[data-reader-column]')?.querySelectorAll('img')).toHaveLength(3);
  });

  // req: EF-LEC-001
  it.each([
    ['no_readable_copy', 409, 'Rien à lire ici'],
    ['full_read_not_allowed', 409, 'Lecture entière refusée'],
    ['excerpt_unavailable', 409, 'Extrait indisponible']
  ])('says what is wrong, with the action, when the server answers %s', async (code, status, title) => {
    server.use(
      http.get(`${BASE}/api/reader/unit/U1/publication`, () =>
        HttpResponse.json(
          { type: `/api/v1/problems/${code}`, title, status, code, action: 'Rien à faire ici.' },
          { status }
        )
      )
    );
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent(title);
    expect(screen.queryByRole('img')).toBeNull();
  });

  // req: EF-LEC-001
  it('tells a hidden book from a missing one in no way at all', async () => {
    server.use(
      http.get(`${BASE}/api/reader/unit/U1/publication`, () =>
        HttpResponse.json({ type: '/api/v1/problems/not_found', title: 'Introuvable', status: 404, code: 'not_found' }, { status: 404 })
      )
    );
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent('Introuvable');
  });

  // req: EF-LEC-001
  it('asks only the kinds of book it can read, and says so for an EPUB', async () => {
    server.use(...serve(publication({ kind: 'epub', pages: [], spine: [{ index: 0, href: '/content/TICKET/c1.xhtml', media_type: 'application/xhtml+xml' }] })));
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent('Ce format n’est pas encore lisible ici');
    expect(asked).toEqual([]);
  });
});
