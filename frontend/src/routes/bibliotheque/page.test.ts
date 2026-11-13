import { render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();
const hash = (n: number) => n.toString(16).padStart(64, '0');
const SERIES_A = { id: '01SERIESAAAAAAAAAAAAAAAAAA', title: 'Saga claire' };
const SERIES_B = { id: '01SERIESBBBBBBBBBBBBBBBBBB', title: 'Saga lointaine' };
const SERIES = [SERIES_A, SERIES_B];
const SHELF = { id: '01SHELFAAAAAAAAAAAAAAAAAAA', name: 'Famille', max_age_rating: 13 };

const work = (n: number) => ({
  work_id: `W${n}`,
  title: `Titre ${n}`,
  kind: 'manga',
  edition_id: `E${n}`,
  cover_hash: hash(n)
});
const me = (role: string) => http.get(`${BASE}/api/v1/me`, () => HttpResponse.json({ id: 'U', nav: [], role }));
const shelves = (items: unknown[] | null) =>
  http.get(`${BASE}/api/v1/shelves`, () =>
    items
      ? HttpResponse.json({ items, since: 'x', removed: [] })
      : HttpResponse.json({ type: '/api/v1/problems/forbidden', title: 'Accès refusé', status: 403, code: 'forbidden' }, { status: 403 })
  );
const seen: URL[] = [];
const works = (answer: (url: URL) => Record<string, unknown>) =>
  http.get(`${BASE}/api/v1/works`, ({ request }) => {
    const url = new URL(request.url);
    seen.push(url);
    return HttpResponse.json(answer(url) as never);
  });

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => {
  server.resetHandlers();
  seen.length = 0;
});
afterAll(() => server.close());

describe('Library screen', () => {
  // req: EF-SRC-039
  it('test_series_filters_per_shelf_and_series', async () => {
    const user = userEvent.setup();
    server.use(
      me('admin'),
      shelves([SHELF]),
      works((url) => {
        const filtered = url.searchParams.has('series_id') || url.searchParams.has('shelf_id');
        return {
          items: filtered ? [work(1)] : [work(1), work(2), work(3)],
          total: filtered ? 1 : 3,
          series: url.searchParams.get('facets') === 'true' ? SERIES : null,
          since: 's',
          removed: []
        };
      })
    );
    render(Page);
    expect(await screen.findByText('3 Œuvres')).toBeInTheDocument();
    expect(seen[0]?.searchParams.get('facets')).toBe('true');
    expect(seen[0]?.searchParams.has('series_id')).toBe(false);

    await user.selectOptions(screen.getByLabelText('Série'), SERIES_A.id);
    await waitFor(() => expect(seen.at(-1)?.searchParams.get('series_id')).toBe(SERIES_A.id));
    expect(seen.at(-1)?.searchParams.has('facets')).toBe(false); // the choices are kept
    expect(await screen.findByText('1 Œuvre')).toBeInTheDocument();
    expect(screen.getAllByRole('listitem')).toHaveLength(1);

    await user.selectOptions(screen.getByLabelText('Étagère'), SHELF.id);
    await waitFor(() => expect(seen.at(-1)?.searchParams.get('shelf_id')).toBe(SHELF.id));
    expect(seen.at(-1)?.searchParams.get('series_id')).toBe(SERIES_A.id);
  });

  // req: EF-SRC-039, EF-USR-006
  it('shows a child no shelf filter, no error and no link to a sheet', async () => {
    server.use(
      me('child'),
      shelves(null),
      works(() => ({ items: [work(1), work(2)], total: 2, series: [SERIES_A], since: 's', removed: [] }))
    );
    render(Page);
    expect(await screen.findByText('2 Œuvres')).toBeInTheDocument();
    expect(screen.queryByLabelText('Étagère')).toBeNull();
    expect(screen.getByLabelText('Série')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryAllByRole('link')).toHaveLength(0);
    expect(screen.getByText('Titre 1')).toBeInTheDocument();
    expect(screen.getByText('Titre 2')).toBeInTheDocument();
    expect(screen.queryByText('Titre 3')).toBeNull();
  });

  // req: EF-BIB-020, ENF-PERF-004
  it('draws 60 covers from thumbnails only, and the admin gets a link to each sheet', async () => {
    server.use(
      me('admin'),
      shelves([]),
      works(() => ({ items: Array.from({ length: 60 }, (_, i) => work(i + 1)), total: 60, series: [], since: 's', removed: [] }))
    );
    const { container } = render(Page);
    expect(await screen.findByText('60 Œuvres')).toBeInTheDocument();
    const images = [...container.querySelectorAll('img')];
    expect(images).toHaveLength(60);
    for (const img of images) {
      expect(img.getAttribute('src')).toMatch(/^\/api\/v1\/thumbnails\/[0-9a-f]{64}\/160$/);
    }
    expect(screen.getByRole('link', { name: 'Titre 5' })).toHaveAttribute('href', '/serie/E5');
  });

  // req: EF-UX-008
  it('offers an action, not a dead end, when there is nothing to show', async () => {
    server.use(me('member'), shelves(null), works(() => ({ items: [], total: 0, series: [], since: 's', removed: [] })));
    render(Page);
    expect(await screen.findByText('Aucune Œuvre à afficher')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Chercher un livre' })).toHaveAttribute('href', '/chercher');
  });

  // req: EF-UX-003
  it('shows the server problem when the list is refused', async () => {
    server.use(
      me('member'),
      shelves(null),
      http.get(`${BASE}/api/v1/works`, () =>
        HttpResponse.json({ type: '/api/v1/problems/resync_required', title: 'Rechargez', status: 409, code: 'resync_required' }, { status: 409 })
      )
    );
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent('Rechargez');
  });
});
