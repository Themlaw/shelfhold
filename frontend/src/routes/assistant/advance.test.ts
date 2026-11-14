import { goto } from '$app/navigation';
import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { t } from '#lib/i18n/index.ts';
import CompteScreen from './compte/+page.svelte';
import EtageresScreen from './etageres/+page.svelte';
import KavitaScreen from './kavita/+page.svelte';

// ENF-UX-001, counted as the journey counts: a gesture is a click or a submission. A screen that
// has nothing left to ask moves on by itself (the link stays for whoever wants it at once), so
// "Continuer" is no action of the budget of 6.

const BASE = 'http://shelfhold.test';
const server = setupServer();
const SOON = { timeout: 4000 };

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
  vi.mocked(goto).mockClear();
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const me = (role: string) =>
  HttpResponse.json({ id: 'U1', username: 'alice', role, nav: [], display_name: 'alice' });

describe('Assistant : le passage d\'un écran au suivant', () => {
  // req: ENF-UX-001
  it('test_account_screen_moves_on_to_kavita_for_the_admin_only', async () => {
    server.use(http.get(`${BASE}/api/v1/me`, () => me('admin')));
    const { unmount } = render(CompteScreen);
    expect(await screen.findByText('Connecté en tant que alice.')).toBeInTheDocument();
    await vi.waitFor(() => expect(goto).toHaveBeenCalledWith('/assistant/kavita'), SOON);
    unmount();
    vi.mocked(goto).mockClear();
    server.use(http.get(`${BASE}/api/v1/me`, () => me('member')));
    render(CompteScreen);
    await screen.findByRole('alert');
    await new Promise((resolve) => setTimeout(resolve, 1800));
    expect(goto).not.toHaveBeenCalled();
  });

  // req: ENF-UX-001
  it('test_kavita_screen_moves_on_to_the_folder_once_connected_and_sends_the_folders_map', async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post(`${BASE}/api/v1/targets`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: 'T1' }, { status: 201 });
      }),
      http.get(`${BASE}/api/v1/setup/state`, () =>
        HttpResponse.json({
          targets: [{ id: 'T1', name: 'kavita', key_expires_at: '2027-02-11T09:00:00Z' }],
          destinations: [],
          first_run: false
        })
      )
    );
    render(KavitaScreen);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(t('admin.wizard.kavita.url')), 'http://kavita:5000');
    await user.type(screen.getByLabelText("Nom d'utilisateur Kavita"), 'admin');
    await user.type(screen.getByLabelText('Mot de passe Kavita'), 'synthetic-kavita-password');
    await user.type(
      screen.getByLabelText('Correspondance des dossiers (avancé)'),
      '/library=/library, /extra=/extra'
    );
    await user.click(screen.getByRole('checkbox', { name: /J'ai compris/ }));
    await user.click(screen.getByRole('button', { name: 'Connecter Kavita' }));
    expect(await screen.findByText(/Kavita est connecté/)).toBeInTheDocument();
    expect(body).toMatchObject({
      path_map: [
        { local: '/library', remote: '/library', mode: 'rw' },
        { local: '/extra', remote: '/extra', mode: 'rw' }
      ]
    });
    await vi.waitFor(() => expect(goto).toHaveBeenCalledWith('/assistant/dossier'), SOON);
  });

  // req: ENF-UX-001
  it('test_kavita_screen_sends_no_map_when_the_field_is_empty_and_refuses_a_bad_one', async () => {
    let body: Record<string, unknown> | null = null;
    let calls = 0;
    server.use(
      http.post(`${BASE}/api/v1/targets`, async ({ request }) => {
        calls += 1;
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: 'T1' }, { status: 201 });
      }),
      http.get(`${BASE}/api/v1/setup/state`, () =>
        HttpResponse.json({ targets: [], destinations: [], first_run: false })
      )
    );
    render(KavitaScreen);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(t('admin.wizard.kavita.url')), 'http://kavita:5000');
    await user.type(screen.getByLabelText("Nom d'utilisateur Kavita"), 'admin');
    await user.type(screen.getByLabelText('Mot de passe Kavita'), 'synthetic-kavita-password');
    await user.click(screen.getByRole('checkbox', { name: /J'ai compris/ }));
    const map = screen.getByLabelText('Correspondance des dossiers (avancé)');
    await user.type(map, 'not-a-pair');
    await user.click(screen.getByRole('button', { name: 'Connecter Kavita' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('local=distant');
    expect(calls).toBe(0); // refused before the password leaves the page
    await user.clear(map);
    await user.click(screen.getByRole('button', { name: 'Connecter Kavita' }));
    expect(await screen.findByText(/Kavita est connecté/)).toBeInTheDocument();
    expect(body).not.toHaveProperty('path_map');
  });

  // req: ENF-UX-001
  it('test_shelves_screen_moves_on_to_the_end_once_the_shelves_are_made', async () => {
    server.use(
      http.get(`${BASE}/api/v1/setup/state`, () =>
        HttpResponse.json({
          targets: [], destinations: [], first_run: false,
          shelves: [{ name: 'Livres', phrase: 'Les livres vont dans Kavita', created: false }],
          sources: [{ key: 'gutenberg', family: false }]
        })
      ),
      http.post(`${BASE}/api/v1/shelves/precreate`, () =>
        HttpResponse.json({ created: ['Livres'] }, { status: 201 })
      ),
      http.put(`${BASE}/api/v1/setup/sources/family`, () =>
        HttpResponse.json({ family: ['gutenberg'] })
      )
    );
    render(EtageresScreen);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Créer les étagères' }));
    expect(await screen.findByText('Étagères créées.')).toBeInTheDocument();
    await vi.waitFor(() => expect(goto).toHaveBeenCalledWith('/assistant/fin'), SOON);
  });
});
