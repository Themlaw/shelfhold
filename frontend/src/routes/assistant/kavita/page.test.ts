import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { t } from '#lib/i18n/index.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const PASSWORD = 'synthetic-kavita-password-0039';
const server = setupServer();

const problem = (status: number, extra: Record<string, unknown> = {}) =>
  HttpResponse.json(
    { type: '/api/v1/problems/x', title: 'Refusé.', status, code: 'x', ...extra },
    { status }
  );
const state = () =>
  HttpResponse.json({
    targets: [{ id: 'T1', name: 'kavita', key_expires_at: '2027-02-11T09:00:00Z' }],
    destinations: [],
    first_run: false
  });

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
});
afterEach(() => {
  server.resetHandlers();
  localStorage.clear();
  sessionStorage.clear();
});
afterAll(() => server.close());

async function fill(accept = true) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(t('admin.wizard.kavita.url')), 'http://kavita:5000');
  await user.type(screen.getByLabelText("Nom d'utilisateur Kavita"), 'admin');
  await user.type(screen.getByLabelText('Mot de passe Kavita'), PASSWORD);
  if (accept) await user.click(screen.getByRole('checkbox'));
  return user;
}

describe('Assistant : connexion à Kavita', () => {
  // req: EF-UX-001
  it('test_kavita_screen_warns_admin_account_required_before_anything_is_typed', () => {
    render(Page);
    expect(screen.getByRole('note')).toHaveTextContent('Compte administrateur requis');
    expect(screen.getByLabelText('Mot de passe Kavita')).toHaveAttribute('type', 'password');
    expect(screen.getByRole('button', { name: 'Connecter Kavita' })).toBeDisabled();
  });

  // req: EF-UX-001
  it('test_kavita_connect_needs_the_risk_to_be_accepted', async () => {
    let calls = 0;
    server.use(http.post(`${BASE}/api/v1/targets`, () => (calls += 1, problem(409))));
    render(Page);
    await fill(false);
    expect(screen.getByRole('button', { name: 'Connecter Kavita' })).toBeDisabled();
    expect(calls).toBe(0);
  });

  // req: EF-UX-001
  it('test_kavita_credentials_sent_once_then_wiped_from_the_screen', async () => {
    let sent: Record<string, unknown> | null = null;
    let csrf: string | null = null;
    server.use(
      http.post(`${BASE}/api/v1/targets`, async ({ request }) => {
        sent = (await request.json()) as Record<string, unknown>;
        csrf = request.headers.get('x-csrf-token');
        return HttpResponse.json({ id: 'T1' }, { status: 201 });
      }),
      http.get(`${BASE}/api/v1/setup/state`, state)
    );
    const { container } = render(Page);
    const user = await fill();
    await user.click(screen.getByRole('button', { name: 'Connecter Kavita' }));
    expect(await screen.findByText(/Kavita est connecté/)).toBeInTheDocument();
    expect(sent).toMatchObject({
      kind: 'kavita', url: 'http://kavita:5000', username: 'admin', password: PASSWORD,
      accept_admin_risk: true
    });
    expect(csrf).toBe('tok123');
    expect(screen.getByText(/La clé expire le/)).toBeInTheDocument();
    // the form is gone, nothing of the password is left in the page, nor in any browser storage
    expect(screen.queryByLabelText('Mot de passe Kavita')).toBeNull();
    expect(container.innerHTML).not.toContain(PASSWORD);
    expect(JSON.stringify({ ...localStorage })).not.toContain(PASSWORD);
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(PASSWORD);
    expect(screen.getByRole('link', { name: 'Continuer' })).toHaveAttribute('href', '/assistant/dossier');
  });

  // req: EF-UX-001
  it('test_kavita_failure_shows_the_problem_and_wipes_the_password', async () => {
    server.use(
      http.post(`${BASE}/api/v1/targets`, () =>
        problem(502, { code: 'target.exchange_failed', title: 'Échange refusé.' })
      )
    );
    const { container } = render(Page);
    const user = await fill();
    await user.click(screen.getByRole('button', { name: 'Connecter Kavita' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Échange refusé.');
    expect(screen.getByLabelText('Mot de passe Kavita')).toHaveValue('');
    expect(container.innerHTML).not.toContain(PASSWORD);
  });

  // req: EF-UX-001, EF-USR-022
  it('test_kavita_asks_for_the_password_in_place_and_sends_the_same_request_again', async () => {
    let attempts = 0;
    const bodies: string[] = [];
    server.use(
      http.post(`${BASE}/api/v1/targets`, async ({ request }) => {
        attempts += 1;
        bodies.push(await request.text());
        if (attempts === 1) {
          return problem(401, { code: 'reauth_required', reauth_methods: ['password'] });
        }
        return HttpResponse.json({ id: 'T1' }, { status: 201 });
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, () =>
        HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' })
      ),
      http.get(`${BASE}/api/v1/setup/state`, state)
    );
    render(Page);
    const user = await fill();
    await user.click(screen.getByRole('button', { name: 'Connecter Kavita' }));
    await user.type(await screen.findByLabelText('Mot de passe'), 'portail');
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    expect(await screen.findByText(/Kavita est connecté/)).toBeInTheDocument();
    expect(attempts).toBe(2);
    expect(bodies[1]).toBe(bodies[0]); // the request the admin made, not another
  });

  // req: EF-UX-001, EF-USR-022
  it('test_kavita_cancelling_the_reauth_wipes_the_credentials', async () => {
    server.use(
      http.post(`${BASE}/api/v1/targets`, () =>
        problem(401, { code: 'reauth_required', reauth_methods: ['password'] })
      )
    );
    render(Page);
    const user = await fill();
    await user.click(screen.getByRole('button', { name: 'Connecter Kavita' }));
    await user.click(await screen.findByRole('button', { name: 'Annuler' }));
    expect(screen.getByLabelText('Mot de passe Kavita')).toHaveValue('');
    expect(screen.getByLabelText("Nom d'utilisateur Kavita")).toHaveValue('');
  });

  // req: EF-UX-001
  it('test_kavita_without_a_session_sends_to_the_sign_in_and_wipes_the_password', async () => {
    server.use(http.post(`${BASE}/api/v1/targets`, () => problem(401)));
    const { container } = render(Page);
    const user = await fill();
    await user.click(screen.getByRole('button', { name: 'Connecter Kavita' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Vous n\'êtes pas connecté');
    expect(container.innerHTML).not.toContain(PASSWORD);
  });
});
