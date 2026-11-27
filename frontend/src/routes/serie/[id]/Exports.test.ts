import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Exports from './Exports.svelte';

const BASE = 'http://shelfhold.test';
const ENDPOINT = `${BASE}/api/v1/editions/ED1/exports`;
const server = setupServer();

const exported = (over: Record<string, unknown> = {}) => ({
  edition_id: 'ED1',
  items: { unit: 12, volume: 0, bundle: 3, adopted: 0, opds_bundle: 2 },
  kepub_items: 5,
  destinations: [
    {
      destination_id: 'D1', target_id: 'T1', target_name: 'Liseuse du salon', target_kind: 'kobo',
      shelf_name: 'Étagère d’essai', enabled: true, exported: 10, pending: 2, failed: 1,
      last_exported_at: '2026-11-20T10:00:00Z'
    }
  ],
  blocks: [
    { profile_id: 'P1', username: 'lecteur', part_index: 0, number: 1, size: 1536, created_at: '2026-11-01T10:00:00Z', last_used_at: null }
  ],
  ...over
});

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Exports', () => {
  // req: EF-INT-007
  it('shows the objects by kind, the KEPUB offered, the destinations and the blocks', async () => {
    server.use(http.get(ENDPOINT, () => HttpResponse.json(exported())));
    render(Exports, { editionId: 'ED1' });
    expect(await screen.findByText('Liseuse du salon')).toBeInTheDocument();
    expect(screen.getByText('KEPUB offerts aux Kobo').nextElementSibling).toHaveTextContent('5');
    expect(screen.getByText('Blocs OPDS').nextElementSibling).toHaveTextContent('2');
    expect(screen.getByText(/10 exportés, 2 en attente, 1 en échec/)).toBeInTheDocument();
    expect(screen.getByText('lecteur')).toBeInTheDocument();
    expect(screen.getByText(/partie 1/)).toBeInTheDocument();
  });

  // req: EF-INT-007
  it('says so when the edition was exported nowhere', async () => {
    server.use(
      http.get(ENDPOINT, () =>
        HttpResponse.json(
          exported({ items: { unit: 0, volume: 0, bundle: 0, adopted: 0, opds_bundle: 0 }, kepub_items: 0, destinations: [], blocks: [] })
        )
      )
    );
    render(Exports, { editionId: 'ED1' });
    expect(await screen.findByText('Cette édition n’a encore été exportée nulle part.')).toBeInTheDocument();
  });

  // req: EF-INT-007
  it('shows an error with a way to try again', async () => {
    let fail = true;
    server.use(
      http.get(ENDPOINT, () => (fail ? HttpResponse.json({ title: 'x', status: 500, code: 'internal' }, { status: 500 }) : HttpResponse.json(exported())))
    );
    render(Exports, { editionId: 'ED1' });
    const user = userEvent.setup();
    expect(await screen.findByText('Les exports n’ont pas pu être lus')).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole('button', { name: 'Réessayer' }));
    expect(await screen.findByText('Liseuse du salon')).toBeInTheDocument();
  });
});
