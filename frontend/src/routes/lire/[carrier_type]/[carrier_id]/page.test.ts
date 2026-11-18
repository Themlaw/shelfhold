import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
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
  series_id: '01HZZZZZZZZZZZZZZZZZZZZZZW',
  spine: [],
  title: 'Une série',
  ...over
});

let puts: Record<string, unknown>[] = [];
let asked: string[] = [];
let settingsPuts: Record<string, unknown>[] = [];
let settingsAnswer: { direction: string | null; comfort: Record<string, string> } = {
  direction: null,
  comfort: {}
};
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
  http.get(`${BASE}/api/reader/settings`, () => HttpResponse.json(settingsAnswer)),
  http.put(`${BASE}/api/reader/settings`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    settingsPuts.push(body);
    // the server answers what is effective now, once the choice is written
    const answer: { direction: string | null; comfort: Record<string, string> } = {
      direction: settingsAnswer.direction,
      comfort: { ...settingsAnswer.comfort }
    };
    if (body.kind === 'direction') answer.direction = String(body.value);
    else answer.comfort[String(body.key)] = String(body.value);
    return HttpResponse.json(answer);
  }),
  http.get(`${BASE}/content/TICKET/:name`, ({ params }) => {
    asked.push(String(params.name));
    return new HttpResponse(new Uint8Array(16), { headers: { 'Content-Type': 'image/jpeg' } });
  })
];

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
  const patched = globalThis.fetch; // the one the mock server answers through
  // a relative link into /content, as the manifest gives it, lives on the instance
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) =>
    patched(typeof input === 'string' && input.startsWith('/') ? `${BASE}${input}` : input, init)
  );
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: () => `blob:t/${Math.random()}`, revokeObjectURL: () => undefined }));
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
  puts = [];
  asked = [];
  settingsPuts = [];
  settingsAnswer = { direction: null, comfort: {} };
  publicationQuery = null;
  state.page.params = { carrier_type: 'unit', carrier_id: 'U1' };
  state.page.url = new URL('http://shelfhold.test/lire/unit/U1');
});
afterEach(() => {
  cleanup(); // the reader says where it was when it is left, while its handlers are still there
  server.resetHandlers();
});
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
    expect(puts[0]?.device_id).toMatch(/^web-/);
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
  // req: EF-LEC-007
  it('writes nothing just because a book was opened: the place of another device is not replaced', async () => {
    server.use(...serve(publication({ reading_direction: 'webtoon' })));
    render(Page);
    await screen.findByAltText('Page 1 sur 3');
    await new Promise((resolve) => setTimeout(resolve, 1200));
    expect(puts).toEqual([]);
  });

  // req: EF-LEC-001
  it('asks for new links, with the same scope, when the tickets ran out during a long reading', async () => {
    const queries: string[] = [];
    let first = true;
    server.use(
      http.get(`${BASE}/api/reader/unit/U1/publication`, ({ request }) => {
        queries.push(new URL(request.url).search);
        const fresh = !first;
        first = false;
        return HttpResponse.json(
          publication({
            pages: [1, 2, 3].map((n) => ({
              index: n - 1,
              href: `/content/${fresh ? 'NEW' : 'OLD'}/p${n}.jpg`,
              media_type: 'image/jpeg'
            }))
          })
        );
      }),
      http.get(`${BASE}/content/OLD/:name`, () => new HttpResponse(null, { status: 404 })),
      http.get(`${BASE}/content/NEW/:name`, () => new HttpResponse(new Uint8Array(16), { headers: { 'Content-Type': 'image/jpeg' } }))
    );
    render(Page);
    await waitFor(() => expect(screen.getByAltText('Page 1 sur 3')).toHaveAttribute('src'));
    expect(queries).toEqual(['', '?scope=full']);
  });

  // req: EF-LEC-006
  it('asks the portée when a comfort setting changes, and writes the answer', async () => {
    server.use(...serve(publication()));
    const user = userEvent.setup();
    render(Page);
    await screen.findByAltText('Page 1 sur 3');
    await user.click(screen.getByRole('button', { name: 'Réglages de lecture' }));
    await user.click(screen.getByRole('radio', { name: 'Pleine largeur' }));
    expect(screen.getByText('Pour cet appareil ou partout ?')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Partout' }));
    await waitFor(() => expect(settingsPuts).toHaveLength(1));
    expect(settingsPuts[0]).toMatchObject({
      kind: 'comfort',
      key: 'fit',
      value: 'width',
      scope: 'everywhere'
    });
    expect('device_id' in (settingsPuts[0] ?? {})).toBe(false);
    // the other answer names the device
    await user.click(screen.getByRole('radio', { name: 'Toute la page à l’écran' }));
    await user.click(screen.getByRole('button', { name: 'Pour cet appareil' }));
    await waitFor(() => expect(settingsPuts).toHaveLength(2));
    expect(settingsPuts[1]).toMatchObject({
      kind: 'comfort',
      key: 'fit',
      value: 'contain',
      scope: 'device'
    });
    expect(typeof settingsPuts[1]?.device_id).toBe('string');
  });

  // req: EF-LEC-006
  it('writes the direction for the series being read, and the book turns around', async () => {
    server.use(...serve(publication()));
    const user = userEvent.setup();
    render(Page);
    await screen.findByAltText('Page 1 sur 3');
    await user.click(screen.getByRole('button', { name: 'Réglages de lecture' }));
    await user.click(screen.getByRole('radio', { name: 'De droite à gauche' }));
    await waitFor(() => expect(settingsPuts).toHaveLength(1));
    expect(settingsPuts[0]).toMatchObject({
      kind: 'direction',
      key: '01HZZZZZZZZZZZZZZZZZZZZZZW',
      value: 'rtl',
      scope: 'everywhere'
    });
    await user.keyboard('{Escape}'); // the panel closes, the focus goes back to the controls
    await user.keyboard('{ArrowLeft}');
    expect(await screen.findByAltText('Page 2 sur 3')).toBeInTheDocument();
  });

  // req: EF-LEC-011
  it('reaches the first and the last page with Home and End', async () => {
    server.use(...serve(publication()));
    const user = userEvent.setup();
    render(Page);
    await screen.findByAltText('Page 1 sur 3');
    await user.keyboard('{ArrowRight}{ArrowRight}');
    expect(await screen.findByAltText('Page 3 sur 3')).toBeInTheDocument();
    await user.keyboard('{Home}');
    expect(await screen.findByAltText('Page 1 sur 3')).toBeInTheDocument();
    await user.keyboard('{End}');
    expect(await screen.findByAltText('Page 3 sur 3')).toBeInTheDocument();
  });

  // req: EF-LEC-011
  it('opens the help with ?, and Escape gives the focus back to the controls', async () => {
    server.use(...serve(publication()));
    const user = userEvent.setup();
    render(Page);
    await screen.findByAltText('Page 1 sur 3');
    await user.keyboard('?');
    expect(await screen.findByRole('heading', { name: 'Aide du lecteur' })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('heading', { name: 'Aide du lecteur' })).toBeNull();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Quitter la lecture' })).toHaveFocus()
    );
  });
});
