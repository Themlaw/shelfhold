import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './Reader.svelte';

import type { Browser } from '#lib/reader/browser.ts';
import type { GuardVerdict } from '#lib/reader/csp-guard.ts';

const engineSpy = vi.hoisted(() => ({
  opened: [] as unknown[],
  created: 0
}));
// the EPUB adapter pulls foliate-js in, which draws in a real browser (e2e/tests/m5_reader_epub.spec.ts)
vi.mock('#lib/reader/engines/foliate.ts', () => ({
  createEpubEngine: () => {
    engineSpy.created += 1;
    return {
      mediaTypes: ['application/epub+zip'],
      guards: { contentFilter: true },
      open: async (view: unknown) => void engineSpy.opened.push(view),
      goTo: () => undefined,
      next: () => undefined,
      previous: () => undefined,
      onLocator: () => () => undefined,
      onEnd: () => () => undefined,
      onError: () => () => undefined,
      destroy: () => undefined
    };
  }
}));

type Props = {
  carrierType: string;
  carrierId: string;
  scope: 'excerpt' | null;
  guard?: () => Promise<GuardVerdict>;
  browser?: Browser | null;
};
let props: Props = {
  carrierType: 'unit',
  carrierId: 'U1',
  scope: null,
  guard: async () => 'ok'
};

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
  pages: [1, 2, 3].map((n) => ({ index: n, href: `/content/TICKET/p${n}.jpg`, media_type: 'image/jpeg' })), // the server counts from 1
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
  publicationQuery = null;
  props = { carrierType: 'unit', carrierId: 'U1', scope: null, guard: async () => 'ok' };
  engineSpy.opened = [];
  engineSpy.created = 0;
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
    render(Page, { props });
    expect(await screen.findByRole('heading', { level: 1, name: /Une série/ })).toBeInTheDocument();
    expect(await screen.findByAltText('Page 1 sur 3')).toBeInTheDocument();
    await user.keyboard('{ArrowRight}');
    expect(await screen.findByAltText('Page 2 sur 3')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Page 2 sur 3');
    await user.click(screen.getByRole('button', { name: 'Page précédente' }));
    expect(await screen.findByAltText('Page 1 sur 3')).toBeInTheDocument();
  });

  // req: EF-LEC-001
  it('says so when the browser refuses to draw a page, instead of leaving an empty screen', async () => {
    server.use(...serve(publication()));
    render(Page, { props });
    const image = await screen.findByAltText('Page 1 sur 3');
    await waitFor(() => expect(image).toHaveAttribute('src'));
    image.dispatchEvent(new Event('error'));
    expect(await screen.findByRole('alert')).toHaveTextContent('Cette page n’a pas pu s’afficher.');
  });

  // req: EF-LEC-001
  it('turns the arrows around for a book read right to left', async () => {
    server.use(...serve(publication({ reading_direction: 'rtl' })));
    const user = userEvent.setup();
    render(Page, { props });
    await screen.findByAltText('Page 1 sur 3');
    await user.keyboard('{ArrowLeft}');
    expect(await screen.findByAltText('Page 2 sur 3')).toBeInTheDocument();
  });

  // req: EF-LEC-007
  it('sends the position it reached, once, against the file the server chose', async () => {
    server.use(...serve(publication()));
    const user = userEvent.setup();
    render(Page, { props });
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
    props = { ...props, scope: 'excerpt' };
    server.use(...serve(publication({ scope: 'excerpt' })));
    const user = userEvent.setup();
    render(Page, { props });
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
    render(Page, { props });
    await screen.findByAltText('Page 1 sur 3');
    expect(screen.queryByRole('link', { name: /Kavita/ })).toBeNull();
  });

  // req: EF-LEC-002
  it('lays out a webtoon as a column', async () => {
    server.use(...serve(publication({ reading_direction: 'webtoon' })));
    const { container } = render(Page, { props });
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
    render(Page, { props });
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
    render(Page, { props });
    expect(await screen.findByRole('alert')).toHaveTextContent('Introuvable');
  });

  const epub = (over: Pub = {}) =>
    publication({
      kind: 'epub',
      pages: [],
      package: '/content/TICKET/OEBPS/content.opf',
      spine: [{ index: 1, href: '/content/TICKET/OEBPS/c1.xhtml', media_type: 'application/xhtml+xml' }],
      ...over
    });
  const chromium126: Browser = { family: 'chromium', version: 126 };

  // req: EF-LEC-003
  it('opens an EPUB with the EPUB engine, through the registry, and fetches nothing itself', async () => {
    server.use(...serve(epub()));
    render(Page, { props: { ...props, browser: chromium126 } });
    await waitFor(() => expect(engineSpy.opened).toHaveLength(1));
    expect(engineSpy.created).toBe(1);
    expect(asked).toEqual([]);
  });

  // req: EF-LEC-003
  it('proposes Kavita, and builds no EPUB engine, below the minimum browser', async () => {
    server.use(...serve(epub()));
    render(Page, { props: { ...props, browser: { family: 'safari', version: 16.3 } } });
    expect(await screen.findByRole('alert')).toHaveTextContent('Kavita');
    expect(engineSpy.created).toBe(0);
  });

  // req: EF-LEC-003
  it('refuses an EPUB when the browser is not known', async () => {
    server.use(...serve(epub()));
    render(Page, { props: { ...props, browser: null } });
    expect(await screen.findByRole('alert')).toHaveTextContent('Kavita');
    expect(engineSpy.created).toBe(0);
  });

  // req: ENF-SEC-004
  it('test_reader_refuses_to_mount_under_spa_csp: nothing is asked, nothing is drawn', async () => {
    let called = 0;
    server.use(
      http.get(`${BASE}/api/reader/unit/U1/publication`, () => {
        called += 1;
        return HttpResponse.json(publication());
      })
    );
    render(Page, { props: { ...props, guard: async () => 'refused' } });
    expect(await screen.findByRole('alert')).toHaveTextContent('politique');
    expect(called).toBe(0);
    expect(engineSpy.created).toBe(0);
    expect(document.querySelector('[data-reader-page]')).toBeNull();
  });

  // req: ENF-SEC-004
  it('test_reader_metadata_rendered_as_text: a title is text, never markup', async () => {
    const title = '<img src=x onerror="window.__owned=1"><b>gras</b>';
    server.use(...serve(publication({ title })));
    const { container } = render(Page, { props });
    await screen.findByAltText('Page 1 sur 3');
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(title);
    expect(container.querySelector('h1 img, h1 b')).toBeNull();
    expect(container.querySelector('[onerror]')).toBeNull();
    expect(document.title).toBe(title);
    expect((window as unknown as Record<string, unknown>).__owned).toBeUndefined();
  });

  // req: ENF-SEC-004
  it('the reader screen never writes markup: no `@html`, no `innerHTML`', async () => {
    const { readFileSync, readdirSync } = await import('node:fs');
    const dir = `${process.cwd()}/src/reader/`;
    for (const name of readdirSync(dir).filter((n) => n.endsWith('.svelte') || (n.endsWith('.ts') && !n.endsWith('.test.ts')))) {
      const text = readFileSync(dir + name, 'utf8');
      expect(text, name).not.toMatch(/@html|innerHTML|outerHTML|insertAdjacentHTML|document\.write/);
    }
  });

  // req: EF-LEC-007
  it('writes nothing just because a book was opened: the place of another device is not replaced', async () => {
    server.use(...serve(publication({ reading_direction: 'webtoon' })));
    render(Page, { props });
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
              index: n,
              href: `/content/${fresh ? 'NEW' : 'OLD'}/p${n}.jpg`,
              media_type: 'image/jpeg'
            }))
          })
        );
      }),
      http.get(`${BASE}/content/OLD/:name`, () => new HttpResponse(null, { status: 404 })),
      http.get(`${BASE}/content/NEW/:name`, () => new HttpResponse(new Uint8Array(16), { headers: { 'Content-Type': 'image/jpeg' } }))
    );
    render(Page, { props });
    await waitFor(() => expect(screen.getByAltText('Page 1 sur 3')).toHaveAttribute('src'));
    expect(queries).toEqual(['', '?scope=full']);
  });
});
