import { goto } from '$app/navigation';
import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

// ENF-UX-001, EF-UX-001: the first account is made on this screen, with the secret that the
// installer put in the compose. Nothing is typed on the host machine.

const BASE = 'http://shelfhold.test';
const server = setupServer();

const signedOut = http.get(`${BASE}/api/v1/me`, () =>
  HttpResponse.json({ status: 401, code: 'unauthenticated' }, { status: 401 })
);
const status = (needs: boolean, configured = true) =>
  http.get(`${BASE}/api/v1/setup/status`, () =>
    HttpResponse.json({ needs_admin: needs, token_configured: configured })
  );

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => vi.mocked(goto).mockClear());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

async function fill(user: ReturnType<typeof userEvent.setup>, token = 'secret-from-compose') {
  await user.type(await screen.findByLabelText("Nom d'utilisateur"), 'owner');
  await user.type(screen.getByLabelText('Mot de passe'), 'correct horse battery staple');
  await user.type(screen.getByLabelText("Secret d'installation"), token);
}

describe('Assistant : le premier compte', () => {
  // req: EF-UX-001, ENF-UX-001
  it('test_a_new_instance_asks_for_the_first_account_with_the_secret_of_the_compose', async () => {
    server.use(signedOut, status(true));
    render(Page);
    expect(await screen.findByLabelText("Nom d'utilisateur")).toBeInTheDocument();
    expect(screen.getByLabelText('Mot de passe')).toHaveAttribute('type', 'password');
    expect(screen.getByLabelText("Secret d'installation")).toHaveAttribute('type', 'password');
    expect(screen.getByText(/SHELFHOLD_SETUP_TOKEN/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Créer le compte' })).toBeInTheDocument();
  });

  // req: EF-UX-001, ENF-UX-001
  it('test_the_account_is_made_one_submission_signs_in_and_the_wizard_moves_on', async () => {
    let body: unknown = null;
    server.use(
      signedOut,
      status(true),
      http.post(`${BASE}/api/v1/setup/admin`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ csrf_token: 'tok' }, { status: 201 });
      })
    );
    render(Page);
    const user = userEvent.setup();
    await fill(user);
    await user.click(screen.getByRole('button', { name: 'Créer le compte' }));
    expect(await screen.findByText('Connecté en tant que owner.')).toBeInTheDocument();
    expect(body).toEqual({
      username: 'owner',
      password: 'correct horse battery staple',
      setup_token: 'secret-from-compose'
    });
    await vi.waitFor(() => expect(goto).toHaveBeenCalledWith('/assistant/kavita'), {
      timeout: 4000
    });
  });

  // req: EF-UX-001, ENF-SEC-008
  it('test_a_wrong_secret_is_said_and_nothing_moves_on', async () => {
    server.use(
      signedOut,
      status(true),
      http.post(`${BASE}/api/v1/setup/admin`, () =>
        HttpResponse.json(
          { status: 403, code: 'setup_token_invalid', title: "Le secret d'installation est incorrect." },
          { status: 403 }
        )
      )
    );
    render(Page);
    const user = userEvent.setup();
    await fill(user, 'nope');
    await user.click(screen.getByRole('button', { name: 'Créer le compte' }));
    expect(await screen.findByRole('alert')).toHaveTextContent("Le secret d'installation est incorrect.");
    await new Promise((resolve) => setTimeout(resolve, 1800));
    expect(goto).not.toHaveBeenCalled();
  });

  // req: EF-UX-001, ENF-SEC-008
  it('test_without_a_secret_in_the_compose_the_screen_says_how_to_set_one_and_offers_no_form', async () => {
    server.use(signedOut, status(true, false));
    render(Page);
    expect(await screen.findByText(/aucun secret d'installation/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Secret d'installation")).toBeNull();
    expect(screen.getByText(/openssl rand -hex 24/)).toBeInTheDocument();
  });

  // req: EF-UX-001
  it('test_an_instance_that_has_its_admin_sends_to_the_sign_in_page_not_to_a_form', async () => {
    server.use(signedOut, status(false));
    render(Page);
    expect(await screen.findByRole('link', { name: /Connexion|Se connecter|Connectez/ })).toHaveAttribute(
      'href',
      '/login'
    );
    expect(screen.queryByLabelText("Secret d'installation")).toBeNull();
  });
});
