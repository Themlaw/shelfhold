import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();
const HINT = 'services:\n  kavita:\n    volumes:\n      - <host path>:/manga/Kavita:ro\n';

const problem = (status: number, extra: Record<string, unknown> = {}) =>
  HttpResponse.json(
    { type: '/api/v1/problems/x', title: 'Refusé.', status, code: 'x', ...extra },
    { status }
  );
const withDestination = (destinations = [{ id: 'D1', target_id: 'T1', adopted: false, linked: false }]) =>
  http.get(`${BASE}/api/v1/setup/state`, () =>
    HttpResponse.json({ targets: [], destinations, first_run: false })
  );
const probeOut = (over: Record<string, unknown> = {}) => ({
  ok: true,
  write: { ok: true, cause: null, failed_step: null },
  kavita_read: true,
  yaml_hint: null,
  ...over
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

describe('Assistant : dossier partagé', () => {
  // req: EF-UX-002
  it('test_probe_success_says_kavita_reads_the_folder', async () => {
    let body: unknown = null;
    server.use(
      withDestination(),
      http.post(`${BASE}/api/v1/setup/probe`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(probeOut());
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Vérifier le dossier' }));
    expect(await screen.findByText('Kavita lit le dossier partagé.')).toBeInTheDocument();
    expect(body).toEqual({ destination_id: 'D1' });
    expect(screen.queryByRole('alert')).toBeNull();
  });

  // req: EF-UX-002
  it('test_probe_failure_shows_the_exact_yaml_block_as_text', async () => {
    server.use(
      withDestination(),
      http.post(`${BASE}/api/v1/setup/probe`, () =>
        HttpResponse.json(probeOut({ ok: false, kavita_read: false, yaml_hint: HINT }))
      )
    );
    const { container } = render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Vérifier le dossier' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Kavita ne lit pas');
    const block = container.querySelector('pre code');
    expect(block?.textContent).toBe(HINT); // byte for byte: it is pasted into a compose file
  });

  // req: EF-UX-002
  it('test_probe_hint_from_the_server_is_never_markup', async () => {
    const hostile = '<img src=x onerror=alert(1)>';
    server.use(
      withDestination(),
      http.post(`${BASE}/api/v1/setup/probe`, () =>
        HttpResponse.json(probeOut({ ok: false, kavita_read: false, yaml_hint: hostile }))
      )
    );
    const { container } = render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Vérifier le dossier' }));
    await screen.findByRole('alert');
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('pre code')?.textContent).toBe(hostile);
  });

  // req: EF-UX-002
  it('test_probe_write_failure_and_no_library_have_their_own_text', async () => {
    server.use(
      withDestination(),
      http.post(`${BASE}/api/v1/setup/probe`, () =>
        HttpResponse.json(
          probeOut({
            ok: false,
            write: { ok: false, cause: 'permission_denied', failed_step: 'write' },
            kavita_read: null,
            yaml_hint: 'services:\n  shelfhold:\n'
          })
        )
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Vérifier le dossier' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('ne peut pas écrire');
    server.use(
      http.post(`${BASE}/api/v1/setup/probe`, () =>
        HttpResponse.json(probeOut({ kavita_read: 'no_library' }))
      )
    );
    await user.click(screen.getByRole('button', { name: 'Vérifier le dossier' }));
    expect(await screen.findByText(/n'a pas encore de bibliothèque/)).toBeInTheDocument();
  });

  // req: EF-UX-001
  it('test_without_a_destination_the_screen_says_so_and_offers_no_action', async () => {
    server.use(withDestination([]));
    render(Page);
    expect(await screen.findByText(/Aucune étagère n'existe encore/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Vérifier le dossier' })).toBeNull();
  });

  // req: EF-UX-001, EF-EXP-051
  it('test_adoption_preview_then_confirmation_on_the_fingerprint_it_showed', async () => {
    let confirmed: Record<string, unknown> | null = null;
    let previewBody: Record<string, unknown> | null = null;
    server.use(
      withDestination(),
      http.post(`${BASE}/api/v1/setup/adoption/preview`, async ({ request }) => {
        previewBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          fingerprint: 'f'.repeat(64), library_ref: '7', series: 2, files: 5,
          child_library: false, roots: [{ path: '/library', present: true }],
          download_dir: { kind: 'outside_library', copy_needed: true, action: 'x' }
        });
      }),
      http.post(`${BASE}/api/v1/setup/adoption/confirm`, async ({ request }) => {
        confirmed = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          adopted_files: 5, already_adopted: 0, series: 2, library_ref: '7', linked: true
        });
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/Dossier de téléchargement/), '/elsewhere');
    await user.click(screen.getByRole('button', { name: 'Voir ce qui serait adopté' }));
    expect(await screen.findByText(/2 séries et 5 fichiers/)).toBeInTheDocument();
    expect(screen.getByText(/télécharge hors de la bibliothèque/)).toBeInTheDocument();
    expect(previewBody).toEqual({ destination_id: 'D1', download_dir: '/elsewhere' });
    expect(confirmed).toBeNull(); // looking changes nothing
    await user.click(screen.getByRole('button', { name: 'Adopter cette bibliothèque' }));
    expect(await screen.findByText(/5 fichiers adoptés/)).toBeInTheDocument();
    expect(confirmed).toEqual({
      destination_id: 'D1', fingerprint: 'f'.repeat(64), library_ref: '7'
    });
    expect(screen.queryByRole('button', { name: 'Adopter cette bibliothèque' })).toBeNull();
  });

  // req: EF-UX-001
  it('test_adoption_on_a_stale_preview_shows_the_problem_and_adopts_nothing', async () => {
    server.use(
      withDestination(),
      http.post(`${BASE}/api/v1/setup/adoption/preview`, () =>
        HttpResponse.json({
          fingerprint: 'a'.repeat(64), library_ref: '7', series: 1, files: 1,
          child_library: false, roots: [], download_dir: null
        })
      ),
      http.post(`${BASE}/api/v1/setup/adoption/confirm`, () =>
        problem(409, { code: 'ops.preview_stale', title: 'Aperçu périmé.' })
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Voir ce qui serait adopté' }));
    await user.click(await screen.findByRole('button', { name: 'Adopter cette bibliothèque' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Aperçu périmé.');
    expect(screen.queryByText(/fichiers? adoptés?/)).toBeNull();
  });

  // req: EF-UX-001
  it('test_new_library_is_made_from_a_typed_name', async () => {
    let body: unknown = null;
    server.use(
      withDestination(),
      http.post(`${BASE}/api/v1/setup/library`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ library_ref: '9', linked: true }, { status: 201 });
      })
    );
    render(Page);
    const user = userEvent.setup();
    const section = (await screen.findByRole('region', { name: 'Ou créer une bibliothèque neuve' }));
    const make = within(section).getByRole('button', { name: 'Créer la bibliothèque' });
    expect(make).toBeDisabled();
    await user.type(within(section).getByLabelText('Nom de la bibliothèque'), ' Mangas ');
    await user.click(make);
    expect(await screen.findByText('Bibliothèque créée et liée.')).toBeInTheDocument();
    expect(body).toEqual({ destination_id: 'D1', name: 'Mangas' });
  });

  // req: EF-UX-001, EF-USR-022
  it('test_a_stale_session_asks_for_the_password_in_place_then_runs_the_probe_again', async () => {
    let attempts = 0;
    server.use(
      withDestination(),
      http.post(`${BASE}/api/v1/setup/probe`, () => {
        attempts += 1;
        return attempts === 1
          ? problem(401, { code: 'reauth_required', reauth_methods: ['password'] })
          : HttpResponse.json(probeOut());
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, () =>
        HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' })
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Vérifier le dossier' }));
    await user.type(await screen.findByLabelText('Mot de passe'), 'portail');
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    expect(await screen.findByText('Kavita lit le dossier partagé.')).toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  // req: EF-UX-001, EF-USR-024
  it('test_a_member_is_refused_by_the_server_and_the_screen_shows_the_problem', async () => {
    server.use(
      http.get(`${BASE}/api/v1/setup/state`, () =>
        problem(403, { code: 'forbidden', title: 'Action refusée.' })
      )
    );
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent('Action refusée.');
    expect(screen.queryByRole('button', { name: 'Vérifier le dossier' })).toBeNull();
  });
});
