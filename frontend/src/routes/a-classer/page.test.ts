import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const WORK = '01JWORK0000000000000000001';
const TOKEN = '1-abcd1234';
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
    expect(await screen.findByText('<img src=x onerror=alert(1)> Titre')).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    const group = screen.getByRole('group', { name: /Âge de/ });
    const radios = within(group).getAllByRole('radio');
    expect(radios.map((r) => (r as HTMLInputElement).value)).toEqual([
      '0+', '10+', '12+', '13+', '16+', '18+', 'X'
    ]);
    for (const radio of radios) expect(radio).toHaveAccessibleName();
    const rate = screen.getByRole('button', { name: 'Classer' });
    expect(rate).toBeDisabled();
    // keyboard: Tab into the group, arrows move, nothing chosen until a key does it
    const user = userEvent.setup();
    await user.tab();
    expect(radios[0]).toHaveFocus();
    await user.keyboard('{ArrowRight}{ArrowRight}{ArrowRight}');
    expect(radios[3]).toBeChecked();
    expect(rate).toBeEnabled();
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
    await screen.findByText('<img src=x onerror=alert(1)> Titre');
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
    await screen.findByText('<img src=x onerror=alert(1)> Titre');
    const user = userEvent.setup();
    await user.click(within(screen.getByRole('group', { name: /Âge de/ })).getByRole('radio', { name: '16+' }));
    await user.click(screen.getByRole('button', { name: 'Classer' }));
    await user.type(await screen.findByLabelText('Mot de passe'), 'secret');
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    expect(await screen.findByText(/est classée : 16\+/)).toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  // req: EF-USR-015
  it('rates the whole list only for the token typed again', async () => {
    let sent: { age: string; confirm: string } | null = null;
    server.use(
      http.get(`${BASE}/api/v1/to-rate`, ({ request }) =>
        listing(new URL(request.url).searchParams.get('age') ? TOKEN : null)
      ),
      http.post(`${BASE}/api/v1/works/rating-batch`, async ({ request }) => {
        sent = (await request.json()) as { age: string; confirm: string };
        return HttpResponse.json({ age: '13+', rated: [] });
      })
    );
    render(Page);
    await screen.findByText('<img src=x onerror=alert(1)> Titre');
    const user = userEvent.setup();
    const batch = screen.getByRole('region', { name: 'Classer tout en lot' });
    await user.click(within(batch).getByRole('radio', { name: '13+' }));
    await user.click(within(batch).getByRole('button', { name: 'Préparer le lot' }));
    const run = await within(batch).findByRole('button', { name: 'Classer tout' });
    expect(run).toBeDisabled();
    await user.type(within(batch).getByLabelText('Jeton de confirmation'), 'wrong');
    expect(run).toBeDisabled();
    await user.clear(within(batch).getByLabelText('Jeton de confirmation'));
    await user.type(within(batch).getByLabelText('Jeton de confirmation'), TOKEN);
    await user.click(run);
    expect(await screen.findByText(/Œuvre classée|Œuvres classées/)).toBeInTheDocument();
    expect(sent).toEqual({ age: '13+', confirm: TOKEN });
  });
});
