import { goto } from '$app/navigation';
import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

// ENF-UX-001: the folder screen is no dead end any more. It makes the shelf bound to Kavita (the
// route of the wizard, which is `shelfhold shelf create` behind the same guards), then moves on.

const BASE = 'http://shelfhold.test';
const server = setupServer();
const TARGET = { id: 'T1', name: 'kavita', key_expires_at: '2027-02-11T09:00:00Z' };

const problem = (status: number, extra: Record<string, unknown> = {}) =>
  HttpResponse.json(
    { type: '/api/v1/problems/x', title: 'Refusé.', status, code: 'x', ...extra },
    { status }
  );
const state = (targets: unknown[], destinations: unknown[] = []) =>
  http.get(`${BASE}/api/v1/setup/state`, () =>
    HttpResponse.json({ targets, destinations, first_run: false })
  );
const MADE = { shelf_id: 'S1', destination_id: 'D1', created: true };

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

async function section() {
  return screen.findByRole('region', { name: "Créer l'étagère liée à Kavita" });
}

describe('Assistant : dossier partagé, l\'étagère liée à Kavita', () => {
  // req: ENF-UX-001
  it('test_folder_screen_offers_the_shelf_linked_to_kavita_with_its_age_and_its_audit', async () => {
    server.use(state([TARGET]));
    render(Page);
    const form = within(await section());
    expect(form.getByLabelText("Nom de l'étagère")).toHaveValue('Livres');
    expect(form.getByLabelText("Dossier de l'étagère")).toHaveValue('livres');
    const age = form.getByLabelText('Âge maximal') as HTMLSelectElement;
    expect(age).toHaveValue('18+'); // an adult shelf unless the admin chooses otherwise
    expect([...age.options].map((o) => o.value)).toEqual(['18+', '13+']);
    // the audit of who may read the library is said before the button is pressed
    expect(form.getByText(/demande à Kavita quels comptes/)).toBeInTheDocument();
    expect(form.getByRole('button', { name: "Créer l'étagère et la lier à Kavita" })).not.toHaveAttribute('aria-disabled');
  });

  // req: ENF-UX-001
  it('test_the_shelf_is_made_with_the_admins_choices_and_the_wizard_moves_on', async () => {
    let body: unknown = null;
    let csrf: string | null = null;
    let made = false;
    server.use(
      http.get(`${BASE}/api/v1/setup/state`, () =>
        HttpResponse.json({
          targets: [TARGET],
          destinations: made
            ? [{ id: 'D1', target_id: 'T1', adopted: false, linked: true }]
            : [],
          first_run: false
        })
      ),
      http.post(`${BASE}/api/v1/setup/shelf`, async ({ request }) => {
        body = await request.json();
        csrf = request.headers.get('x-csrf-token');
        made = true;
        return HttpResponse.json(MADE, { status: 201 });
      })
    );
    render(Page);
    const user = userEvent.setup();
    const form = within(await section());
    await user.selectOptions(form.getByLabelText('Âge maximal'), '13+');
    await user.click(form.getByRole('button', { name: "Créer l'étagère et la lier à Kavita" }));
    expect(await screen.findByText('Étagère créée et liée à Kavita.')).toBeInTheDocument();
    expect(body).toEqual({ name: 'Livres', target: 'kavita', max_age: '13+', subpath: 'livres' });
    expect(csrf).toBe('tok123');
    // the screen reads the server again: the checks of the folder are there now
    expect(await screen.findByRole('button', { name: 'Vérifier le dossier' })).toBeInTheDocument();
    // no gesture needed to go on: the wizard moves to the next screen by itself
    await vi.waitFor(() => expect(goto).toHaveBeenCalledWith('/assistant/etageres'), {
      timeout: 4000
    });
  });

  // req: ENF-UX-001, EF-EXP-055
  it('test_a_refusal_of_the_kavita_access_audit_is_shown_and_nothing_moves_on', async () => {
    server.use(
      state([TARGET]),
      http.post(`${BASE}/api/v1/setup/shelf`, () =>
        problem(409, { code: 'target.child_exposure', title: 'Des comptes enfants liraient cette bibliothèque.' })
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(
      within(await section()).getByRole('button', { name: "Créer l'étagère et la lier à Kavita" })
    );
    expect(await screen.findByRole('alert')).toHaveTextContent('Des comptes enfants liraient');
    expect(screen.queryByText('Étagère créée et liée à Kavita.')).toBeNull();
    await new Promise((resolve) => setTimeout(resolve, 1800));
    expect(goto).not.toHaveBeenCalled();
  });

  // req: ENF-UX-001, EF-USR-022
  it('test_a_stale_session_asks_for_the_password_in_place_then_makes_the_shelf', async () => {
    let attempts = 0;
    server.use(
      state([TARGET]),
      http.post(`${BASE}/api/v1/setup/shelf`, () => {
        attempts += 1;
        return attempts === 1
          ? problem(401, { code: 'reauth_required', reauth_methods: ['password'] })
          : HttpResponse.json(MADE, { status: 201 });
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, () =>
        HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' })
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(
      within(await section()).getByRole('button', { name: "Créer l'étagère et la lier à Kavita" })
    );
    await user.type(await screen.findByLabelText('Mot de passe'), 'portail');
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    expect(await screen.findByText('Étagère créée et liée à Kavita.')).toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  // req: ENF-UX-001, EF-USR-024
  it('test_a_member_or_a_child_is_refused_by_the_server_and_sees_the_problem', async () => {
    server.use(
      state([TARGET]),
      http.post(`${BASE}/api/v1/setup/shelf`, () =>
        problem(403, { code: 'forbidden', title: 'Action refusée.' })
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(
      within(await section()).getByRole('button', { name: "Créer l'étagère et la lier à Kavita" })
    );
    expect(await screen.findByRole('alert')).toHaveTextContent('Action refusée.');
    expect(goto).not.toHaveBeenCalled();
  });

  // req: ENF-UX-001, ENF-SEC-004
  it('test_a_folder_that_is_not_one_plain_name_is_refused_before_any_request', async () => {
    let calls = 0;
    server.use(
      state([TARGET]),
      http.post(`${BASE}/api/v1/setup/shelf`, () => {
        calls += 1;
        return HttpResponse.json(MADE, { status: 201 });
      })
    );
    render(Page);
    const user = userEvent.setup();
    const form = within(await section());
    const folder = form.getByLabelText("Dossier de l'étagère");
    const make = form.getByRole('button', { name: "Créer l'étagère et la lier à Kavita" });
    for (const bad of ['../dehors', '/etc', 'a/b', '.cache', '']) {
      await user.clear(folder);
      if (bad) await user.type(folder, bad);
      expect(make, `"${bad}"`).toHaveAttribute('aria-disabled', 'true');
    }
    expect(calls).toBe(0);
  });

  // req: ENF-UX-001
  it('test_without_a_kavita_target_the_screen_sends_back_instead_of_a_dead_end', async () => {
    server.use(state([]));
    render(Page);
    expect(await screen.findByText(/Kavita n'est pas encore connecté/)).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: "Créer l'étagère liée à Kavita" })).toBeNull();
    expect(screen.getByRole('link', { name: 'Retour à la connexion à Kavita' })).toHaveAttribute(
      'href',
      '/assistant/kavita'
    );
  });

  // req: ENF-UX-001
  it('test_a_shelf_already_linked_offers_the_checks_not_a_second_shelf', async () => {
    server.use(state([TARGET], [{ id: 'D1', target_id: 'T1', adopted: false, linked: true }]));
    render(Page);
    expect(await screen.findByRole('button', { name: 'Vérifier le dossier' })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: "Créer l'étagère liée à Kavita" })).toBeNull();
  });
});
