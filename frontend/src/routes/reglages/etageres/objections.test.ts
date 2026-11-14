import { render, screen, within } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Destinations from '../destinations/+page.svelte';
import Shelves from './+page.svelte';

// The review of L45 (m5, M2): « Détacher » names every shelf of the target it detaches, and a
// destination the server could not count is listed, said so, and still removable.
const BASE = 'http://shelfhold.test';
const server = setupServer();
const TEMPLATE = { chapter: 'c', volume: 'v', special: 's', special_dir: 'd', segment: 'g' };
const destination = (id: string, shelf: string, over: Record<string, unknown> = {}) => ({
  id,
  shelf_id: `shelf-${id}`,
  shelf_name: shelf,
  target_id: 'T1',
  root_subpath: `${shelf.toLowerCase()}/`,
  transport: 'tree',
  adopted: false,
  flagged_names: 0,
  flagged_reason: null,
  template: TEMPLATE,
  ...over
});

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('the review of L45', () => {
  it('says on each « Détacher » that the whole target goes, and names its shelves', async () => {
    server.use(
      http.get(`${BASE}/api/v1/destinations`, () =>
        HttpResponse.json({ items: [destination('D1', 'Manga'), destination('D2', 'Romans')] })
      )
    );
    render(Destinations);
    const row = (await screen.findByText(/manga\//)).closest('li') as HTMLElement;
    const button = within(row).getByRole('button', { name: 'Détacher Kavita' });
    expect(button).toHaveAccessibleDescription(/toute la cible Kavita.*Manga, Romans/);
  });

  it('lists a destination whose names could not be counted, and says so', async () => {
    server.use(
      http.get(`${BASE}/api/v1/shelves`, () =>
        HttpResponse.json({ items: [{ id: 'shelf-D1', name: 'Manga', max_age_rating: null }] })
      ),
      http.get(`${BASE}/api/v1/destinations`, () =>
        HttpResponse.json({
          items: [destination('D1', 'Manga', { flagged_names: null, flagged_reason: 'TypeError' })]
        })
      )
    );
    render(Shelves);
    const row = (await screen.findByText(/manga\//)).closest('li') as HTMLElement;
    expect(row).toHaveTextContent(/n'ont pas pu être comptés/);
    expect(within(row).getByText('Aperçu du nommage').closest('details')).not.toHaveAttribute('open');
  });
});
