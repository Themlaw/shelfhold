import { render, screen, waitFor, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();

const entry = (id: number, over: Record<string, unknown> = {}) => ({
  event_id: id,
  type: 'series.tombstoned_upstream',
  visibility: 'ops',
  occurred_at: '2026-11-16T09:00:00Z',
  title: null,
  title_masked: false,
  payload: { count: 1 },
  ...over
});
const block = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  source_key: 'source-one',
  ref: 'a1b2c3d4e5f6',
  title: 'Série synthétique',
  number: '12',
  reason: 'bad_quality',
  created_at: '2026-11-15T09:00:00Z',
  proposed_count: 3,
  last_proposed_at: '2026-11-16T08:00:00Z',
  ...over
});
const history = (items: unknown[], next: number | null = null) =>
  http.get(`${BASE}/api/v1/ops/activity-history`, () => HttpResponse.json({ items, next_before: next }));
const blocks = (items: unknown[]) =>
  http.get(`${BASE}/api/v1/ops/blocklist`, () => HttpResponse.json({ items }));
const forbidden = () =>
  HttpResponse.json(
    { type: '/api/v1/problems/forbidden', title: 'Accès refusé', status: 403, code: 'forbidden' },
    { status: 403 }
  );

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Activity history', () => {
  // req: EF-INT-019
  it('lists the events with their stable code and their date', async () => {
    server.use(history([entry(2), entry(1, { title: 'Une série', payload: { count: 4 } })]), blocks([]));
    render(Page);
    const region = await screen.findByRole('region', { name: 'Événements' });
    expect(within(region).getAllByText('series.tombstoned_upstream')).toHaveLength(2);
    expect(within(region).getByText('Une série')).toBeInTheDocument();
    expect(within(region).getAllByRole('listitem')).toHaveLength(2);
  });

  // req: EF-INT-019
  it('loads the older events with the cursor the server gave, and stops when there is none', async () => {
    const seen: (string | null)[] = [];
    server.use(
      http.get(`${BASE}/api/v1/ops/activity-history`, ({ request }) => {
        const before = new URL(request.url).searchParams.get('before');
        seen.push(before);
        return before === null
          ? HttpResponse.json({ items: [entry(5)], next_before: 5 })
          : HttpResponse.json({ items: [entry(4, { type: 'follow.stale' })], next_before: null });
      }),
      blocks([])
    );
    render(Page);
    await screen.findByText('series.tombstoned_upstream');
    await userEvent.click(screen.getByRole('button', { name: 'Voir les plus anciens' }));
    expect(await screen.findByText('follow.stale')).toBeInTheDocument();
    expect(seen).toEqual([null, '5']);
    expect(screen.queryByRole('button', { name: 'Voir les plus anciens' })).toBeNull();
  });

  // req: EF-INT-019
  it('says there is nothing yet when the history is empty', async () => {
    server.use(history([]), blocks([]));
    render(Page);
    expect(await screen.findByText("Rien dans l'historique pour l'instant.")).toBeInTheDocument();
  });

  // req: EF-INT-019
  it('lists the blocked chapters by title and number, with why and how often they came back', async () => {
    server.use(history([]), blocks([block('B1'), block('B2', { title: null, number: null, reason: 'duplicate', proposed_count: 0 })]));
    render(Page);
    const region = await screen.findByRole('region', { name: 'Chapitres bloqués' });
    const items = within(region).getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(within(items[0]).getByText(/Série synthétique/)).toBeInTheDocument();
    expect(within(items[0]).getByText(/Mauvaise qualité/)).toBeInTheDocument();
    expect(within(items[0]).getByText(/proposé de nouveau 3 fois et ignoré/i)).toBeInTheDocument();
    expect(within(items[1]).getByText(/Chapitre inconnu ici/)).toBeInTheDocument();
    expect(within(items[1]).getByText(/Doublon/)).toBeInTheDocument();
  });

  // req: EF-INT-019
  it('says when no chapter is blocked', async () => {
    server.use(history([]), blocks([]));
    render(Page);
    expect(await screen.findByText('Aucun chapitre bloqué.')).toBeInTheDocument();
  });

  // req: EF-INT-019
  it('never unblocks in silence: a first click asks, only the confirmation removes the block', async () => {
    const removed: string[] = [];
    let listing = [block('B1')];
    server.use(
      history([]),
      http.get(`${BASE}/api/v1/ops/blocklist`, () => HttpResponse.json({ items: listing })),
      http.delete(`${BASE}/api/v1/ops/blocklist/:id`, ({ params }) => {
        removed.push(String(params.id));
        listing = [];
        return new HttpResponse(null, { status: 204 });
      })
    );
    render(Page);
    await userEvent.click(await screen.findByRole('button', { name: 'Débloquer' }));
    expect(removed).toEqual([]); // asked, not done
    expect(screen.getByText(/de nouveau accepté/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Annuler' }));
    expect(removed).toEqual([]);
    await userEvent.click(screen.getByRole('button', { name: 'Débloquer' }));
    await userEvent.click(screen.getByRole('button', { name: 'Confirmer le déblocage' }));
    await waitFor(() => expect(removed).toEqual(['B1']));
    expect(await screen.findByRole('status')).toHaveTextContent('Chapitre débloqué');
    expect(await screen.findByText('Aucun chapitre bloqué.')).toBeInTheDocument();
  });

  // req: EF-INT-019
  it('shows the refusal, and nothing of the history, to a member or a child', async () => {
    server.use(
      http.get(`${BASE}/api/v1/ops/activity-history`, forbidden),
      http.get(`${BASE}/api/v1/ops/blocklist`, forbidden)
    );
    render(Page);
    expect((await screen.findAllByRole('alert'))[0]).toHaveTextContent('Accès refusé');
    expect(screen.queryByRole('region', { name: 'Événements' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Chapitres bloqués' })).toBeNull();
  });

  // req: ENF-SEC-013
  it('never shows an identifier of the source, a path or an address', async () => {
    server.use(history([entry(1)]), blocks([block('B1')]));
    const { container } = render(Page);
    await screen.findByRole('region', { name: 'Chapitres bloqués' });
    expect(container.textContent).not.toMatch(/\/srv\/|https?:\/\/|\d+\.\d+\.\d+\.\d+/);
    expect(container.textContent).not.toContain('remote_unit_id');
  });
});
