import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();

const entry = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  title: `Titre ${id}`,
  target_kind: 'source_ref',
  visibility: 'household',
  requester_id: 'U1',
  requester_role: 'member',
  needs_age: false,
  created_at: '2026-11-10T09:00:00Z',
  ...over
});
const queue = (...items: unknown[]) => http.get(`${BASE}/api/v1/requests/queue`, () => HttpResponse.json({ items }));
const problem = (status: number, code: string, title: string) =>
  HttpResponse.json({ type: `/api/v1/problems/${code}`, title, status, code }, { status });
const view = (over: Record<string, unknown> = {}) => ({
  confirmation_required: false, current_age: null, current_x: false, decided_age: null,
  decided_x: false, edition_id: null, needs_age: false, resolved: true, shelf_id: null,
  size_bytes: 10, units: 1, units_minimum: false, ...over
});

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('To approve screen', () => {
  // req: EF-USR-010
  it('lists what waits, flags a child request, and masks the title the server withheld', async () => {
    server.use(
      queue(
        entry('A', { requester_role: 'child', needs_age: true }),
        entry('B', { title: null, visibility: 'private' })
      )
    );
    render(Page);
    expect(await screen.findByRole('button', { name: /Titre A/ })).toBeInTheDocument();
    expect(screen.getByText('Âge à choisir')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Titre masqué/ })).toBeInTheDocument();
    expect(screen.queryByText('B')).toBeNull();
  });

  // req: EF-USR-010
  it('shows nothing of the queue to a role the server refuses', async () => {
    server.use(http.get(`${BASE}/api/v1/requests/queue`, () => problem(403, 'forbidden', 'Accès refusé')));
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent('Accès refusé');
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.queryByText('Aucune demande à approuver')).toBeNull();
  });

  // req: EF-UX-008
  it('an empty queue says so and does not send an admin to search', async () => {
    server.use(queue());
    render(Page);
    expect(await screen.findByText('Aucune demande à approuver')).toBeInTheDocument();
  });

  // req: EF-USR-010
  it('opens the approval sheet of the chosen request and reloads the queue once approved', async () => {
    let approved = false;
    server.use(
      http.get(`${BASE}/api/v1/requests/queue`, () =>
        HttpResponse.json({ items: approved ? [] : [entry('A')] })
      ),
      http.get(`${BASE}/api/v1/requests/A/approval`, () => HttpResponse.json(view())),
      http.post(`${BASE}/api/v1/requests/A/approve`, () => {
        approved = true;
        return HttpResponse.json({ id: 'A', state: 'approved' });
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /Titre A/ }));
    await user.click(await screen.findByRole('button', { name: 'Approuver' }));
    expect(await screen.findByText('Aucune demande à approuver')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Approuver une demande' })).toBeNull();
  });

  // req: EF-USR-010
  it('refuses with a reason (never empty), sends the CSRF token, and reloads', async () => {
    let sent: { body: unknown; csrf: string | null } | null = null;
    server.use(
      queue(entry('A')),
      http.get(`${BASE}/api/v1/requests/A/approval`, () => HttpResponse.json(view())),
      http.post(`${BASE}/api/v1/requests/A/reject`, async ({ request }) => {
        sent = { body: await request.json(), csrf: request.headers.get('x-csrf-token') };
        return HttpResponse.json({ id: 'A', state: 'rejected' });
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /Titre A/ }));
    const refuse = await screen.findByRole('button', { name: 'Refuser' });
    expect(refuse).toBeDisabled();
    await user.type(screen.getByLabelText('Motif du refus'), 'Déjà possédé');
    await user.click(refuse);
    await vi.waitFor(() => expect(sent).not.toBeNull());
    expect(sent).toEqual({ body: { reason: 'Déjà possédé' }, csrf: 'tok123' });
  });

  // req: EF-USR-010
  it('marks a request not found, with the CSRF token', async () => {
    let csrf: string | null = null;
    server.use(
      queue(entry('A')),
      http.get(`${BASE}/api/v1/requests/A/approval`, () => HttpResponse.json(view())),
      http.post(`${BASE}/api/v1/requests/A/not-found`, ({ request }) => {
        csrf = request.headers.get('x-csrf-token');
        return HttpResponse.json({ id: 'A', state: 'not_found' });
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /Titre A/ }));
    await user.click(await screen.findByRole('button', { name: 'Introuvable' }));
    await vi.waitFor(() => expect(csrf).toBe('tok123'));
  });

  // req: EF-USR-010
  it('a refused decision is shown and the request stays in the queue', async () => {
    server.use(
      queue(entry('A')),
      http.get(`${BASE}/api/v1/requests/A/approval`, () => HttpResponse.json(view())),
      http.post(`${BASE}/api/v1/requests/A/not-found`, () => problem(409, 'request.not_pending', 'Déjà décidée'))
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /Titre A/ }));
    await user.click(await screen.findByRole('button', { name: 'Introuvable' }));
    expect(await screen.findByText('Déjà décidée')).toBeInTheDocument();
    const list = screen.getByRole('list');
    expect(within(list).getByRole('button', { name: /Titre A/ })).toBeInTheDocument();
  });
});
