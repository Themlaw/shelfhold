import { render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import Page from './+page.svelte';

// Import a reading list (EF-USR-020): `POST /api/v1/request-lists` (one's own) and
// `POST /api/v1/request-lists/for-child/{id}` (the admin's, a reserved action), `GET` the batches.
const BASE = 'http://shelfhold.test';
const LISTS = `${BASE}/api/v1/request-lists`;
const server = setupServer();
const KID = '01JKID00000000000000000000';
const profile = (id: string, username: string, role: string) => ({
  id, username, role, can_rate: false, hide_unrated_remote: true, max_age_rating: null,
  subject_filter: [], visibility: 'household'
});
const done = (over: Record<string, unknown> = {}) => ({
  id: '01JLIST0000000000000000000', in_library: 2, requested: 3, annotated: 0, collection_id: null, ...over
});
const world = (profiles: unknown[] = [], batches: unknown[] = []) =>
  server.use(
    http.get(`${BASE}/api/v1/profiles`, () => HttpResponse.json({ items: profiles })),
    http.get(LISTS, () => HttpResponse.json({ items: batches }))
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

describe('List import screen', () => {
  // req: EF-USR-020
  it('sends the text as it is, tells what was done and links to the one review screen', async () => {
    world();
    let sent: unknown = null;
    server.use(
      http.post(LISTS, async ({ request }) => {
        sent = await request.json();
        return HttpResponse.json(done({ annotated: 2 }), { status: 201 });
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Liste'), 'Un titre{Enter}Un autre ; 9780306406157');
    await user.type(screen.getByLabelText('Nom de la liste'), 'Été');
    await user.click(screen.getByRole('button', { name: 'Importer' }));
    expect(await screen.findByText(/2 déjà dans la bibliothèque, 3 demandés/)).toBeInTheDocument();
    expect(sent).toEqual({ text: 'Un titre\nUn autre ; 9780306406157', name: 'Été' });
    expect(screen.getByText(/2 lignes réclament une édition annotée précise/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Valider toute la liste' })).toHaveAttribute(
      'href',
      '/a-approuver/liste/01JLIST0000000000000000000'
    );
  });

  // req: EF-USR-020
  it('offers a child as the profile of the list and asks again for the password in place', async () => {
    world([profile(KID, 'noa', 'child'), profile('01JADM', 'admin', 'admin')]);
    let attempts = 0;
    server.use(
      http.post(`${LISTS}/for-child/${KID}`, () => {
        attempts += 1;
        return attempts === 1
          ? HttpResponse.json(
              { type: '/api/v1/problems/reauth-required', title: 'Reconnexion', status: 401, code: 'reauth_required', reauth_methods: ['password'] },
              { status: 401 }
            )
          : HttpResponse.json(done(), { status: 201 });
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, () => HttpResponse.json({}))
    );
    render(Page);
    const user = userEvent.setup();
    const select = await screen.findByLabelText('Pour');
    expect(screen.queryByRole('option', { name: 'admin' })).toBeNull(); // only the children
    await user.selectOptions(select, KID);
    expect(screen.getByText(/chaque demande demandera un âge/)).toBeInTheDocument();
    await user.type(screen.getByLabelText('Liste'), 'Un titre');
    await user.click(screen.getByRole('button', { name: 'Importer' }));
    await user.type(await screen.findByLabelText(/mot de passe/i), 'x');
    await user.click(screen.getByRole('button', { name: /confirmer|valider/i }));
    expect(await screen.findByText(/2 déjà dans la bibliothèque/)).toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  // req: EF-USR-020
  it('shows the refusal of a list the server does not read, and keeps the text', async () => {
    world();
    server.use(
      http.post(LISTS, () =>
        HttpResponse.json(
          { type: '/api/v1/problems/request.invalid_list', title: 'Cette liste ne peut pas être lue.', status: 422, code: 'request.invalid_list', reason: 'too_many_lines' },
          { status: 422 }
        )
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Liste'), 'Un titre');
    await user.click(screen.getByRole('button', { name: 'Importer' }));
    expect(await screen.findByText('Cette liste ne peut pas être lue.')).toBeInTheDocument();
    expect((screen.getByLabelText('Liste') as HTMLTextAreaElement).value).toBe('Un titre');
  });

  // req: EF-USR-020
  it('lists the batches already imported', async () => {
    world([], [
      { id: '01JB1', name: 'Été', user_id: 'U', user_role: 'admin', created_at: '2026-11-10T09:00:00Z', requested: 4, in_library: 1 }
    ]);
    render(Page);
    const link = await screen.findByRole('link', { name: 'Été' });
    expect(link).toHaveAttribute('href', '/a-approuver/liste/01JB1');
    await waitFor(() => expect(screen.getByText('1 dans la bibliothèque, 4 demandés')).toBeInTheDocument());
  });
});
