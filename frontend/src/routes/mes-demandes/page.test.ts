import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const KAVITA = 'http://kavita.test:5000/series/7';
const server = setupServer();

class QuietSource {
  constructor(public url: string) {}
  addEventListener() {}
  close() {}
  onerror = null;
}

const row = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  state: 'pending',
  title: `Titre ${id}`,
  reject_reason: null,
  created_at: '2026-11-10T09:00:00Z',
  updated_at: '2026-11-10T09:00:00Z',
  family_state: 'waiting',
  open_url: null,
  ...over
});
const listing = (...items: unknown[]) => http.get(`${BASE}/api/v1/requests`, () => HttpResponse.json({ items }));

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
  vi.stubGlobal('EventSource', QuietSource);
});
afterEach(() => {
  server.resetHandlers();
  vi.unstubAllGlobals();
});
afterAll(() => server.close());

describe('My requests screen', () => {
  // req: EF-USR-010
  it('test_my_requests_shows_reject_reason', async () => {
    server.use(
      listing(
        row('A', { state: 'rejected', family_state: null, reject_reason: 'Déjà dans la bibliothèque' }),
        row('B', { state: 'not_found', family_state: null }),
        row('C', { state: 'cancelled', family_state: null })
      )
    );
    render(Page);
    const refused = (await screen.findByText('Titre A')).closest('article') as HTMLElement;
    expect(within(refused).getByText('Refusée')).toBeInTheDocument();
    expect(within(refused).getByText('Motif : Déjà dans la bibliothèque')).toBeInTheDocument();
    expect(screen.getByText('Introuvable')).toBeInTheDocument();
    expect(screen.getByText('Annulée')).toBeInTheDocument();
    // a closed request offers nothing to cancel
    expect(screen.queryByRole('button', { name: 'Annuler' })).toBeNull();
  });

  // req: EF-USR-010
  it('test_member_sees_available_in_kavita_with_link', async () => {
    server.use(listing(row('A', { state: 'arrived', family_state: 'available', open_url: KAVITA })));
    render(Page);
    expect(await screen.findByText('Disponible dans Kavita')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Ouvrir dans Kavita' })).toHaveAttribute('href', KAVITA);
    expect(screen.queryByRole('button', { name: 'Annuler' })).toBeNull();
  });

  // req: EF-USR-010
  it('never turns a title or a reason into markup, nor a non-web address into a link', async () => {
    server.use(
      listing(
        row('A', {
          title: '<img src=x onerror=alert(1)>',
          state: 'rejected',
          family_state: null,
          reject_reason: '<b>non</b>'
        }),
        row('B', { family_state: 'available', open_url: 'javascript:alert(1)' })
      )
    );
    const { container } = render(Page);
    expect(await screen.findByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
    expect(container.querySelector('img, b')).toBeNull();
    expect(screen.queryByRole('link')).toBeNull();
  });

  // req: EF-USR-010
  it('shows the one family word, never a technical step', async () => {
    server.use(
      listing(row('A', { family_state: 'preparing', state: 'approved' }), row('B', { family_state: 'waiting' }))
    );
    render(Page);
    expect(await screen.findByText('En préparation')).toBeInTheDocument();
    expect(screen.getByText('En attente')).toBeInTheDocument();
    expect(screen.queryByText(/pending|approved|Étape/)).toBeNull();
  });

  // req: EF-USR-010
  it('cancels a waiting request with the CSRF token, then reloads the list', async () => {
    let cancelled = false;
    let csrf: string | null = null;
    server.use(
      http.get(`${BASE}/api/v1/requests`, () =>
        HttpResponse.json({
          items: [cancelled ? row('A', { state: 'cancelled', family_state: null }) : row('A')]
        })
      ),
      http.post(`${BASE}/api/v1/requests/:id/cancel`, ({ request, params }) => {
        csrf = request.headers.get('x-csrf-token');
        cancelled = params.id === 'A';
        return HttpResponse.json(row('A', { state: 'cancelled', family_state: null }));
      })
    );
    render(Page);
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Annuler' }));
    expect(await screen.findByText('Annulée')).toBeInTheDocument();
    expect(csrf).toBe('tok123');
    expect(screen.queryByRole('button', { name: 'Annuler' })).toBeNull();
  });

  // req: EF-USR-010
  it('a refused cancellation is shown, not swallowed', async () => {
    server.use(
      listing(row('A')),
      http.post(`${BASE}/api/v1/requests/:id/cancel`, () =>
        HttpResponse.json(
          { type: '/api/v1/problems/request.not_cancellable', title: 'Annulation impossible', status: 409, code: 'request.not_cancellable' },
          { status: 409 }
        )
      )
    );
    render(Page);
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Annuler' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Annulation impossible');
  });

  // req: EF-UX-008
  it('an empty list is a state with an action', async () => {
    server.use(listing());
    render(Page);
    expect(await screen.findByText('Aucune demande en cours')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Chercher un livre' })).toHaveAttribute('href', '/chercher');
  });

  // req: EF-UX-003
  it('an unreadable answer shows a problem card, not an empty list', async () => {
    server.use(
      http.get(`${BASE}/api/v1/requests`, () =>
        HttpResponse.json({ type: '/api/v1/problems/unauthorized', title: 'Connexion requise', status: 401, code: 'unauthorized' }, { status: 401 })
      )
    );
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent('Connexion requise');
    expect(screen.queryByText('Aucune demande en cours')).toBeNull();
  });
});
