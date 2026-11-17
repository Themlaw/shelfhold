import { render, screen, waitFor, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import Page from './+page.svelte';

vi.mock('$app/state', () => ({ page: { params: { id: '01JCOLL000000000000000000A' } } }));

// One collection or list, in order (EF-BIB-016, L33b): `GET|DELETE /api/v1/collections/{id}`,
// `DELETE …/items/{item}`. The order is the one the server numbers (from 1, among what the reader
// is shown); a list that is not the reader's is the same 404 as one nobody ever had.
const BASE = 'http://shelfhold.test';
const API = `${BASE}/api/v1/collections/01JCOLL000000000000000000A`;
const server = setupServer();
const entry = (position: number, title: string, over: Record<string, unknown> = {}) => ({
  id: `01JITEM00000000000000000${position}`, position, work_id: `01JWORK00000000000000000${position}`,
  title, number: null, cover_hash: null, ...over
});
const detail = (over: Record<string, unknown> = {}) => ({
  id: '01JCOLL000000000000000000A', kind: 'reading_list', name: 'À lire cet été', scope: 'profile', mine: true,
  count: 2, cover_hash: null, created_at: '2026-11-10T10:00:00Z',
  items: [entry(1, 'Atlas des marées', { number: '12' }), entry(2, 'Le Jardin des brumes')], ...over
});
const world = (body: Record<string, unknown>, role = 'member') =>
  server.use(
    http.get(`${BASE}/api/v1/me`, () => HttpResponse.json({ id: 'U', username: 'mum', role, nav: [] })),
    http.get(API, () => HttpResponse.json(body))
  );

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
  setUiLanguage('fr');
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Collection', () => {
  // req: EF-BIB-016
  it('draws the items in the order the server numbered them, with their number', async () => {
    world(detail());
    render(Page);
    expect(await screen.findByRole('heading', { name: 'À lire cet été', level: 1 })).toBeTruthy();
    const rows = within(screen.getByRole('list', { name: 'Éléments' })).getAllByRole('listitem');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('1');
    expect(rows[0]).toHaveTextContent('Atlas des marées');
    expect(rows[0]).toHaveTextContent('12');
    expect(rows[1]).toHaveTextContent('Le Jardin des brumes');
    // no route shows a Work by itself (the sheet is of an Edition): the title is text, not a link
    expect(within(rows[0]!).queryByRole('link')).toBeNull();
  });

  // req: EF-BIB-016
  it('writes "Liste sans nom" when the name is withheld and says nothing of what is hidden', async () => {
    world(detail({ name: null, mine: false, scope: 'shared', kind: 'collection', items: [entry(1, 'Atlas des marées')], count: 1 }));
    render(Page);
    expect(await screen.findByRole('heading', { name: 'Liste sans nom', level: 1 })).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/masqué|caché|hidden|manquant/i);
    expect(screen.queryByRole('button', { name: 'Retirer' })).toBeNull(); // not hers to change
  });

  // req: EF-BIB-016
  it('is the same "not found" for a list that is not hers and for one that does not exist', async () => {
    server.use(
      http.get(API, () =>
        HttpResponse.json({ type: '/api/v1/problems/not_found', status: 404, code: 'not_found', title: 'Introuvable', cause: 'c', action: 'a' }, { status: 404 })
      )
    );
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent('Introuvable');
    expect(screen.queryByRole('heading', { level: 1, name: /À lire/ })).toBeNull();
  });

  // req: EF-BIB-016
  it('removes an item from her own list and reloads', async () => {
    world(detail());
    let removed = '';
    server.use(
      http.delete(`${API}/items/:item`, ({ params }) => {
        removed = String(params.item);
        world(detail({ count: 1, items: [entry(1, 'Le Jardin des brumes')] }));
        return new HttpResponse(null, { status: 204 });
      })
    );
    render(Page);
    const rows = within(await screen.findByRole('list', { name: 'Éléments' })).getAllByRole('listitem');
    await userEvent.click(within(rows[0]!).getByRole('button', { name: /Retirer/ }));
    await waitFor(() => expect(removed).toBe('01JITEM000000000000000001'));
    await waitFor(() => expect(within(screen.getByRole('list', { name: 'Éléments' })).getAllByRole('listitem')).toHaveLength(1));
  });

  // req: EF-BIB-016
  it('asks twice before deleting a list, then goes back to the list of lists', async () => {
    world(detail());
    let deleted = false;
    server.use(
      http.delete(API, () => {
        deleted = true;
        return new HttpResponse(null, { status: 204 });
      })
    );
    render(Page);
    await userEvent.click(await screen.findByRole('button', { name: 'Supprimer la liste' }));
    expect(deleted).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Confirmer la suppression' }));
    await waitFor(() => expect(deleted).toBe(true));
  });

  // req: EF-BIB-016
  it('lets an admin remove from a household collection but not a member', async () => {
    world(detail({ scope: 'shared', mine: false, kind: 'collection' }), 'member');
    const first = render(Page);
    await screen.findByRole('heading', { name: 'À lire cet été', level: 1 });
    expect(screen.queryByRole('button', { name: /Retirer/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Supprimer la liste' })).toBeNull();
    first.unmount();
    world(detail({ scope: 'shared', mine: false, kind: 'collection' }), 'admin');
    render(Page);
    expect((await screen.findAllByRole('button', { name: /Retirer/ })).length).toBe(2);
  });
});
