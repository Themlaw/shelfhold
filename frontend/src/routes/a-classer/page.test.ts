import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { delay, http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const WORK = '01JWORK0000000000000000001';
const server = setupServer();

const listing = (token: string | null = null) =>
  HttpResponse.json({
    age: token ? '13+' : null,
    token,
    items: [
      { work_id: WORK, title: '<img src=x onerror=alert(1)> Titre', state: 'rating_pending', reason: 'r' }
    ]
  });
const problem = (status: number, extra: Record<string, unknown> = {}) =>
  HttpResponse.json(
    { type: '/api/v1/problems/x', title: 'x', status, code: 'x', ...extra },
    { status }
  );

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('À classer', () => {
  // req: EF-USR-015
  it('test_to_rate_screen_minimal_list_and_selector', async () => {
    server.use(http.get(`${BASE}/api/v1/to-rate`, () => listing()));
    const { container } = render(Page);
    // the title is text: no element is made out of it
    const found = await screen.findAllByText('<img src=x onerror=alert(1)> Titre');
    expect(found.length).toBeGreaterThan(0);
    expect(container.querySelector('img')).toBeNull();
    const group = screen.getByRole('group', { name: /Âge de/ });
    const radios = within(group).getAllByRole('radio');
    expect(radios.map((r) => (r as HTMLInputElement).value)).toEqual([
      '0+', '10+', '12+', '13+', '16+', '18+', 'X'
    ]);
    for (const radio of radios) expect(radio).toHaveAccessibleName();
    const rate = screen.getByRole('button', { name: 'Classer' });
    expect(rate).toHaveAttribute('aria-disabled', 'true');
    // keyboard: Tab into the group, arrows move, nothing chosen until a key does it
    const user = userEvent.setup();
    await user.tab(); // the two filters come first: the state, then the search
    await user.tab();
    await user.tab();
    expect(radios[0]).toHaveFocus();
    await user.keyboard('{ArrowRight}{ArrowRight}{ArrowRight}');
    expect(radios[3]).toBeChecked();
    expect(rate).not.toHaveAttribute('aria-disabled');
  });

  // req: EF-USR-015
  it('test_to_rate_screen_requires_session_and_csrf', async () => {
    let seen: Request | null = null;
    server.use(
      http.get(`${BASE}/api/v1/to-rate`, () => listing()),
      http.post(`${BASE}/api/v1/works/:id/rating`, ({ request }) => {
        seen = request;
        return HttpResponse.json({
          target: 't', label: 'l', effective: 13, effective_x: false,
          withdrawals: [{ edition_id: 'E1', shelf_id: 'S1', reason: 'above_ceiling' }]
        });
      })
    );
    render(Page);
    await screen.findAllByText('<img src=x onerror=alert(1)> Titre');
    const user = userEvent.setup();
    await user.click(within(screen.getByRole('group', { name: /Âge de/ })).getByRole('radio', { name: '13+' }));
    await user.click(screen.getByRole('button', { name: 'Classer' }));
    expect(await screen.findByText(/Rien n'a été retiré/)).toBeInTheDocument();
    expect(screen.getByText(/Édition E1, étagère S1/)).toBeInTheDocument();
    expect((seen as Request | null)?.headers.get('x-csrf-token')).toBe('tok123');
  });

  // req: EF-USR-015
  it('shows a sign-in message without a session', async () => {
    server.use(http.get(`${BASE}/api/v1/to-rate`, () => problem(401)));
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent('Connexion requise');
  });

  // req: EF-USR-015, EF-USR-022
  it('asks for the password in place and runs the same rating again', async () => {
    let attempts = 0;
    server.use(
      http.get(`${BASE}/api/v1/to-rate`, () => listing()),
      http.post(`${BASE}/api/v1/works/:id/rating`, async ({ request }) => {
        attempts += 1;
        const body = (await request.json()) as { age: string };
        if (attempts === 1) {
          return problem(401, {
            code: 'reauth_required', auth_age_s: 2000, max_age_s: 900, reauth_methods: ['password']
          });
        }
        expect(body.age).toBe('16+');
        return HttpResponse.json({
          target: 't', label: 'l', effective: 16, effective_x: false, withdrawals: []
        });
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, () =>
        HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' })
      )
    );
    render(Page);
    await screen.findAllByText('<img src=x onerror=alert(1)> Titre');
    const user = userEvent.setup();
    await user.click(within(screen.getByRole('group', { name: /Âge de/ })).getByRole('radio', { name: '16+' }));
    await user.click(screen.getByRole('button', { name: 'Classer' }));
    await user.type(await screen.findByLabelText('Mot de passe'), 'secret');
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    expect(await screen.findByText(/est classée : 16\+/)).toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  // req: EF-USR-006
  it('rates one Edition alone from its suggestion line', async () => {
    let seen: { age: string } | null = null;
    let path = '';
    server.use(
      http.get(`${BASE}/api/v1/to-rate`, () =>
        HttpResponse.json({
          age: null,
          token: null,
          items: [
            {
              work_id: WORK, title: 'Alpha', state: 'suggestion_above', reason: 'r',
              suggestions: [{ edition_id: '01JEDIT0000000000000000009', age: 16, x: false, provenance: 'source-a' }]
            }
          ]
        })
      ),
      http.post(`${BASE}/api/v1/editions/:id/rating`, async ({ request }) => {
        path = new URL(request.url).pathname;
        seen = (await request.json()) as { age: string };
        return HttpResponse.json({
          target: 't', label: 'l', effective: 16, effective_x: false, withdrawals: []
        });
      })
    );
    render(Page);
    await screen.findAllByText('Alpha');
    const user = userEvent.setup();
    const edition = screen.getByRole('group', { name: /Âge de l'Édition 01JEDIT0000000000000000009/ });
    await user.click(within(edition).getByRole('radio', { name: '16+' }));
    await user.click(screen.getByRole('button', { name: "Classer l'Édition seule" }));
    expect(await screen.findByText(/Édition classée/)).toBeInTheDocument();
    expect(path).toBe('/api/v1/editions/01JEDIT0000000000000000009/rating');
    expect(seen).toEqual({ age: '16+' });
  });

  // req: EF-USR-006, ENF-SEC-010
  it('shows nothing of the list to a member or a child (403) and never the titles', async () => {
    server.use(http.get(`${BASE}/api/v1/to-rate`, () => problem(403, { code: 'forbidden' })));
    const { container } = render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent(/réservé|refusé/i);
    expect(container.querySelector('ul.works')).toBeNull();
    expect(screen.queryByRole('region', { name: 'Classement en masse' })).toBeNull();
  });

  // req: EF-USR-015
  it('says so when a filter matches nothing and clears the filters on request', async () => {
    server.use(http.get(`${BASE}/api/v1/to-rate`, () => listing()));
    render(Page);
    await screen.findAllByText('<img src=x onerror=alert(1)> Titre');
    const user = userEvent.setup();
    await user.type(screen.getByRole('searchbox', { name: 'Chercher dans la liste' }), 'zzz');
    expect(screen.getByText('Aucune Œuvre ne correspond aux filtres.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Effacer les filtres' }));
    expect(screen.queryByText('Aucune Œuvre ne correspond aux filtres.')).toBeNull();
    expect(screen.getByRole('searchbox', { name: 'Chercher dans la liste' })).toHaveValue('');
  });

  // req: EF-USR-015
  it('shows a loading state while the list is being read', async () => {
    server.use(
      http.get(`${BASE}/api/v1/to-rate`, async () => {
        await delay(100);
        return listing();
      })
    );
    render(Page);
    expect(screen.getByRole('status')).toHaveTextContent('Chargement des Œuvres à classer');
    await screen.findAllByText('<img src=x onerror=alert(1)> Titre');
  });
});
