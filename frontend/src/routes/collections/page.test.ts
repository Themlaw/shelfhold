import { render, screen, waitFor, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import Page from './+page.svelte';

// Collections and reading lists (EF-BIB-016, L33b): `GET|POST /api/v1/collections`, `POST
// …/import-cbl`. The server decides what a reader may see: the screen draws it, writes a list
// without a name as "Liste sans nom" and never guesses what is hidden.
const BASE = 'http://shelfhold.test';
const server = setupServer();
const ME = '01JUSER000000000000000000M';
const item = (over: Record<string, unknown> = {}) => ({
  id: '01JCOLL000000000000000000A', kind: 'reading_list', name: 'À lire cet été', scope: 'profile', mine: true,
  count: 3, cover_hash: null, created_at: '2026-11-10T10:00:00Z', ...over
});
const world = (items: unknown[], role = 'member') =>
  server.use(
    http.get(`${BASE}/api/v1/me`, () => HttpResponse.json({ id: ME, username: 'mum', role, nav: [] })),
    http.get(`${BASE}/api/v1/collections`, () => HttpResponse.json({ items }))
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

describe('Collections', () => {
  // req: EF-BIB-016
  it('lists the collections and the lists with their count and a link to each', async () => {
    world([item(), item({ id: '01JCOLL000000000000000000B', kind: 'collection', name: 'Maison', scope: 'shared', mine: false, count: 1 })]);
    render(Page);
    expect(await screen.findByRole('heading', { name: 'Collections et listes', level: 1 })).toBeTruthy();
    const mine = (await screen.findByRole('link', { name: 'À lire cet été' })) as HTMLAnchorElement;
    expect(mine.getAttribute('href')).toBe('/collections/01JCOLL000000000000000000A');
    expect(screen.getByRole('link', { name: 'Maison' })).toBeTruthy();
    expect(screen.getByText(/3 éléments/)).toBeTruthy();
    expect(screen.getByText(/1 élément(?!s)/)).toBeTruthy();
  });

  // req: EF-BIB-016
  it('writes a list the server withheld the name of as "Liste sans nom", and invents nothing', async () => {
    world([item({ name: null, scope: 'shared', mine: false, kind: 'collection', count: 4 })]);
    render(Page);
    const link = await screen.findByRole('link', { name: 'Liste sans nom' });
    expect(link).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/null|undefined/);
    expect(screen.getByText(/4 éléments/)).toBeTruthy(); // the count the server gives, nothing more
  });

  // req: EF-BIB-016
  it('says so when there is nothing yet and offers to start a list', async () => {
    world([]);
    render(Page);
    expect(await screen.findByText(/Aucune liste pour l'instant/)).toBeTruthy();
    expect(screen.getByLabelText('Nom de la liste')).toBeTruthy();
  });

  // req: EF-BIB-016
  it('creates a private list with the name typed, as a reading list', async () => {
    world([]);
    let body: unknown = null;
    server.use(
      http.post(`${BASE}/api/v1/collections`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ id: '01JCOLL000000000000000000C' }, { status: 201 });
      })
    );
    render(Page);
    await userEvent.type(await screen.findByLabelText('Nom de la liste'), 'Mes classiques');
    await userEvent.click(screen.getByRole('button', { name: 'Créer la liste' }));
    await waitFor(() => expect(body).toEqual({ name: 'Mes classiques', kind: 'reading_list', scope: 'profile' }));
  });

  // req: EF-BIB-016
  it('offers the household scope to an admin only', async () => {
    world([], 'member');
    const first = render(Page);
    await screen.findByLabelText('Nom de la liste');
    expect(screen.queryByLabelText('Portée')).toBeNull();
    first.unmount();
    world([], 'admin');
    render(Page);
    expect(await screen.findByLabelText('Portée')).toBeTruthy();
  });

  // req: EF-BIB-016
  it('imports a CBL file and lists the lines it could not resolve, without guessing', async () => {
    world([]);
    server.use(
      http.post(`${BASE}/api/v1/collections/import-cbl`, () =>
        HttpResponse.json(
          {
            collection: item(), imported: 2,
            unresolved: [
              { index: 1, series: 'Série A', number: '3', reason: 'not_found' },
              { index: 4, series: 'Série B', number: '1', reason: 'ambiguous' }
            ]
          },
          { status: 201 }
        )
      )
    );
    render(Page);
    const file = new File(['<ReadingList/>'], 'liste.cbl', { type: 'text/xml' });
    await userEvent.upload(await screen.findByLabelText('Importer un fichier CBL'), file);
    const done = await screen.findByRole('status');
    expect(done).toHaveTextContent('2');
    const lines = within(screen.getByRole('list', { name: 'Lignes non résolues' })).getAllByRole('listitem');
    expect(lines).toHaveLength(2);
    expect(lines[0]).toHaveTextContent('Série A');
    expect(lines[0]).toHaveTextContent('introuvable');
    expect(lines[1]).toHaveTextContent('plusieurs');
  });

  // req: EF-BIB-016
  it('shows the problem of a refused file as the server wrote it', async () => {
    world([]);
    server.use(
      http.post(`${BASE}/api/v1/collections/import-cbl`, () =>
        HttpResponse.json(
          { type: '/api/v1/problems/collection.cbl_refused', status: 422, code: 'collection.cbl_refused', title: 'titre refusé', cause: 'cause refusée', action: 'action refusée', reason: 'too_big' },
          { status: 422 }
        )
      )
    );
    render(Page);
    await userEvent.upload(await screen.findByLabelText('Importer un fichier CBL'), new File(['x'], 'a.cbl'));
    expect(await screen.findByRole('alert')).toHaveTextContent('titre refusé');
  });

  // req: EF-BIB-016
  it('draws the same in English', async () => {
    setUiLanguage('en');
    world([item({ name: null })]);
    render(Page);
    expect(await screen.findByRole('heading', { name: 'Collections and lists', level: 1 })).toBeTruthy();
    expect(await screen.findByRole('link', { name: 'Unnamed list' })).toBeTruthy();
  });
});
