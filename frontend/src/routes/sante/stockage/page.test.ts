import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();

const board = (over: Record<string, unknown> = {}) => ({
  shelves: [
    {
      shelf_id: '01SHELFA',
      name: 'Étagère A',
      bytes: 157_286_400,
      copies: 12,
      by_location: { store: 104_857_600, library: 52_428_800, destination: 0, adopted_ro: 0 }
    },
    { shelf_id: '01SHELFB', name: 'Étagère B', bytes: 0, copies: 0, by_location: {} },
    { shelf_id: null, name: null, bytes: 2048, copies: 1, by_location: { store: 2048 } }
  ],
  traffic: {
    days: 30,
    sources: [
      {
        source_key: 'source-one',
        requests: 5,
        bytes_in: 4_010,
        by_day: [
          { day: '2026-11-15', requests: 1, bytes_in: 10 },
          { day: '2026-11-16', requests: 4, bytes_in: 4_000 }
        ]
      }
    ]
  },
  ...over
});
const serve = (body: unknown, seen: (string | null)[] = []) =>
  http.get(`${BASE}/api/v1/ops/storage-dashboard`, ({ request }) => {
    seen.push(new URL(request.url).searchParams.get('days'));
    return HttpResponse.json(body as never);
  });

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Storage and network dashboard', () => {
  // req: EF-INT-020
  it('shows the space of each shelf, with the shelves that hold nothing and what is filed nowhere', async () => {
    server.use(serve(board()));
    render(Page);
    const region = await screen.findByRole('region', { name: 'Espace par étagère' });
    const rows = within(region).getAllByRole('row');
    expect(rows).toHaveLength(4); // the header and three lines
    expect(within(rows[1] as HTMLElement).getByText('Étagère A')).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText(/150\s*Mo/)).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText('12')).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText('Étagère B')).toBeInTheDocument();
    expect(within(rows[3] as HTMLElement).getByText('Hors étagère')).toBeInTheDocument();
    expect(within(rows[3] as HTMLElement).getAllByText(/2\s*ko/i)).toHaveLength(2); // its size, and the store column
  });

  // req: EF-INT-020
  it('shows the traffic of each source, with its days', async () => {
    server.use(serve(board()));
    render(Page);
    const region = await screen.findByRole('region', { name: 'Trafic par source' });
    expect(within(region).getByRole('heading', { name: 'source-one' })).toBeInTheDocument();
    expect(within(region).getByText(/5 requêtes/)).toBeInTheDocument();
    const days = within(region).getAllByRole('row');
    expect(days.length).toBeGreaterThanOrEqual(3); // the header and two days
  });

  // req: EF-INT-020
  it('asks for the window the person picks', async () => {
    const seen: (string | null)[] = [];
    server.use(serve(board(), seen));
    render(Page);
    await screen.findByRole('region', { name: 'Trafic par source' });
    await userEvent.selectOptions(screen.getByLabelText('Période'), '7');
    await screen.findByRole('region', { name: 'Trafic par source' });
    expect(seen).toEqual(['30', '7']);
  });

  // req: EF-INT-020
  it('says so when no source has made a request in the window', async () => {
    server.use(serve(board({ traffic: { days: 30, sources: [] } })));
    render(Page);
    expect(await screen.findByText('Aucune requête sur cette période.')).toBeInTheDocument();
  });

  // req: EF-INT-020
  it('explains what is counted and what is not', async () => {
    server.use(serve(board()));
    render(Page);
    expect(await screen.findByText(/sans adresse ni chemin/)).toBeInTheDocument();
  });

  // req: EF-INT-020
  it('shows the refusal and nothing of the dashboard to a member or a child', async () => {
    server.use(
      http.get(`${BASE}/api/v1/ops/storage-dashboard`, () =>
        HttpResponse.json(
          { type: '/api/v1/problems/forbidden', title: 'Accès refusé', status: 403, code: 'forbidden' },
          { status: 403 }
        )
      )
    );
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent('Accès refusé');
    expect(screen.queryByRole('region', { name: 'Espace par étagère' })).toBeNull();
  });

  // req: ENF-SEC-013
  it('never shows a path, a host or an address', async () => {
    server.use(serve(board()));
    const { container } = render(Page);
    await screen.findByRole('region', { name: 'Trafic par source' });
    expect(container.textContent).not.toMatch(/\/srv\/|https?:\/\/|\d+\.\d+\.\d+\.\d+|rel_path/);
  });
});
