import { render, screen, waitFor, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { goto } from '$app/navigation';
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
  vi.mocked(goto).mockClear();
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
    expect(await screen.findByText('3 œuvres')).toBeInTheDocument();
    expect(seen[0]?.searchParams.get('facets')).toBe('true');
    expect(seen[0]?.searchParams.has('series_id')).toBe(false);

    await user.selectOptions(screen.getByLabelText('Série'), SERIES_A.id);
    await waitFor(() => expect(seen.at(-1)?.searchParams.get('series_id')).toBe(SERIES_A.id));
    expect(seen.at(-1)?.searchParams.has('facets')).toBe(false); // the choices are kept
    expect(await screen.findByText('1 œuvre')).toBeInTheDocument();
    expect(within(screen.getByRole('list', { name: 'Couvertures' })).getAllByRole('listitem')).toHaveLength(1);

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
    expect(await screen.findByText('2 œuvres')).toBeInTheDocument();
    expect(screen.queryByLabelText('Étagère')).toBeNull();
    expect(screen.getByLabelText('Série')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
    // the only link is her own saved selections (M5 L21); none goes to a sheet
    expect(screen.queryAllByRole('link').map((l) => l.getAttribute('href')?.endsWith('/selections'))).toEqual([true]);
    expect(screen.getByRole('heading', { name: 'Titre 1' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Titre 2' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Titre 3' })).toBeNull();
  });

  // req: EF-BIB-020, ENF-PERF-004
  it('draws a page of covers from thumbnails only, and the admin gets a link to each sheet', async () => {
    server.use(
      me('admin'),
      shelves([]),
      works(() => ({ items: Array.from({ length: 24 }, (_, i) => work(i + 1)), total: 24, series: [], since: 's', removed: [] }))
    );
    const { container } = render(Page);
    expect(await screen.findByText('24 œuvres')).toBeInTheDocument();
    const images = [...container.querySelectorAll('img')];
    expect(images).toHaveLength(24);
    for (const img of images) {
      expect(img.getAttribute('src')).toMatch(/^\/api\/v1\/thumbnails\/[0-9a-f]{64}\/320$/);
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

  // req: EF-BIB-020
  it('composes the cover under the picture and names the author and the kind under the title', async () => {
    server.use(
      me('member'),
      shelves(null),
      works(() => ({
        items: [{ ...work(1), author: 'Jules Verne', kind: 'manga' }, { ...work(2), cover_hash: null }],
        total: 2,
        series: [],
        since: 's',
        removed: []
      }))
    );
    const { container } = render(Page);
    expect(await screen.findByText('2 œuvres')).toBeInTheDocument();
    // every volume has its composed cover, with or without a picture over it
    expect(container.querySelectorAll('.cover[data-family]')).toHaveLength(2);
    expect(container.querySelectorAll('.cover[data-image="pending"]')).toHaveLength(1);
    expect(screen.getAllByText('Jules Verne').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Manga').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Auteur inconnu').length).toBeGreaterThan(0);
  });

  // req: EF-BIB-020
  it('sorts, switches to a list, and reflects the view in the address', async () => {
    const user = userEvent.setup();
    server.use(
      me('admin'),
      shelves([SHELF]),
      works(() => ({ items: [work(1), work(2)], total: 2, series: SERIES, since: 's', removed: [] }))
    );
    render(Page);
    expect(await screen.findByText('2 œuvres')).toBeInTheDocument();
    expect(seen[0]?.searchParams.get('sort')).toBe('title');

    await user.selectOptions(screen.getByLabelText('Trier par'), 'recent');
    await waitFor(() => expect(seen.at(-1)?.searchParams.get('sort')).toBe('recent'));
    expect(goto).toHaveBeenLastCalledWith(expect.stringMatching(/\?tri=recent$/), expect.anything());

    await user.click(screen.getByLabelText('Liste'));
    expect(goto).toHaveBeenLastCalledWith(expect.stringMatching(/\?tri=recent&vue=liste$/), expect.anything());
    expect(screen.getByRole('list', { name: 'Couvertures' }).tagName).toBe('UL');
    expect(screen.getByRole('link', { name: 'Titre 2' })).toHaveAttribute('href', '/serie/E2');
  });

  // req: EF-SRC-039
  it('shows each active filter as a chip that clears itself', async () => {
    const user = userEvent.setup();
    server.use(
      me('admin'),
      shelves([SHELF]),
      works((url) => ({
        items: [work(1)],
        total: 1,
        series: url.searchParams.get('facets') === 'true' ? SERIES : null,
        since: 's',
        removed: []
      }))
    );
    render(Page);
    expect(await screen.findByText('1 œuvre')).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('Série'), SERIES_A.id);
    const chip = await screen.findByRole('button', { name: /Série : Saga claire/ });
    expect(goto).toHaveBeenLastCalledWith(expect.stringContaining(`?series=${SERIES_A.id}`), expect.anything());
    await user.click(chip);
    await waitFor(() => expect(seen.at(-1)?.searchParams.has('series_id')).toBe(false));
    expect(screen.queryByRole('button', { name: /Série : Saga claire/ })).toBeNull();
  });

  // req: EF-BIB-020
  it('pages the list: a counter, then the next page asks for the next slice', async () => {
    const user = userEvent.setup();
    server.use(
      me('member'),
      shelves(null),
      works((url) => ({
        items: Array.from({ length: 24 }, (_, i) => work(i + 1 + Number(url.searchParams.get('offset') ?? 0))),
        total: 60,
        series: [],
        since: 's',
        removed: []
      }))
    );
    render(Page);
    expect(await screen.findByText('Œuvres 1 à 24 sur 60')).toBeInTheDocument();
    expect(seen[0]?.searchParams.get('limit')).toBe('24');
    await user.click(screen.getByRole('button', { name: 'Page suivante' }));
    expect(await screen.findByText('Œuvres 25 à 48 sur 60')).toBeInTheDocument();
    expect(seen.at(-1)?.searchParams.get('offset')).toBe('24');
    expect(goto).toHaveBeenLastCalledWith(expect.stringMatching(/\?page=2$/), expect.anything());
  });
});
