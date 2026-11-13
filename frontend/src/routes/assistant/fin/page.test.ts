import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import EtageresPage from '../etageres/+page.svelte';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();
const PHRASE = 'lampadaire bleu vingt-trois';
const AGE_FILE = '-----BEGIN AGE ENCRYPTED FILE-----\nYWdlLWVuY3J5cHRpb24ub3JnL3YxCg\n-----END AGE ENCRYPTED FILE-----\n';

const problem = (status: number, extra: Record<string, unknown> = {}) =>
  HttpResponse.json(
    { type: '/api/v1/problems/x', title: 'Refusé.', status, code: 'x', ...extra },
    { status }
  );
/** The state the server answers; `calls` lets a test make it change after an action. */
const stateOf = (over: () => Record<string, unknown> = () => ({})) =>
  http.get(`${BASE}/api/v1/setup/state`, () =>
    HttpResponse.json({
      targets: [],
      destinations: [],
      first_run: false,
      shelves: [],
      sources: [],
      keyring_exported: false,
      jurisdiction: { country: 'FR', confirmed: false },
      ...over()
    })
  );
const blobText = (blob: Blob): Promise<string> =>
  new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.readAsText(blob);
  });

const downloads: { blob: Blob; name: string }[] = [];
let lastBlob: Blob | null = null;

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
  downloads.length = 0;
  lastBlob = null;
  URL.createObjectURL = vi.fn((blob: Blob) => {
    lastBlob = blob;
    return 'blob:keyring';
  });
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    if (lastBlob) downloads.push({ blob: lastBlob, name: this.download });
  });
});
afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
});
afterAll(() => server.close());

describe('Assistant : fin', () => {
  // req: EF-UX-001, EF-SRC-024
  it('test_wizard_confirms_jurisdiction', async () => {
    let body: unknown = null;
    let confirmed = false;
    server.use(
      stateOf(() => ({ jurisdiction: { country: 'BE', confirmed } })),
      http.patch(`${BASE}/api/v1/config`, async ({ request }) => {
        body = await request.json();
        confirmed = true;
        return HttpResponse.json({ key: 'instance.jurisdiction', value: 'BE' });
      })
    );
    render(Page);
    const user = userEvent.setup();
    expect(await screen.findByText(/Pays détecté : BE/)).toBeInTheDocument();
    expect(screen.queryByText('Juridiction confirmée.')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Confirmer la juridiction' }));
    expect(await screen.findByText('Juridiction confirmée.')).toBeInTheDocument();
    expect(body).toEqual({ values: { 'instance.jurisdiction': 'BE' } }); // the server's own guess
    expect(screen.queryByRole('button', { name: 'Confirmer la juridiction' })).toBeNull();
  });

  // req: EF-UX-001, EF-SRC-024
  it('test_already_confirmed_jurisdiction_is_shown_not_asked_again', async () => {
    server.use(stateOf(() => ({ jurisdiction: { country: 'FR', confirmed: true } })));
    render(Page);
    expect(await screen.findByText('Juridiction confirmée.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Confirmer la juridiction' })).toBeNull();
  });

  // req: EF-UX-001, EF-INT-015, ENF-SEC-005
  it('test_wizard_forces_keyring_export_before_finish', async () => {
    let exported = false;
    let sent: unknown = null;
    server.use(
      stateOf(() => ({ keyring_exported: exported, jurisdiction: { country: 'FR', confirmed: true } })),
      http.post(`${BASE}/api/v1/setup/keyring-export`, async ({ request }) => {
        sent = await request.json();
        exported = true;
        return new HttpResponse(AGE_FILE, {
          headers: { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' }
        });
      })
    );
    const { container } = render(Page);
    const user = userEvent.setup();
    const field = await screen.findByLabelText('Phrase de passe du trousseau');
    // nothing to finish with while the server says the keyring was not exported
    expect(screen.queryByRole('link', { name: 'Terminer' })).toBeNull();
    const exportButton = screen.getByRole('button', { name: 'Exporter le trousseau' });
    expect(exportButton).toBeDisabled(); // no phrase yet
    await user.type(field, 'trop court');
    expect(exportButton).toBeDisabled(); // under twelve characters: no request leaves
    await user.clear(field);
    await user.type(field, PHRASE);
    await user.click(exportButton);
    expect(await screen.findByRole('link', { name: 'Terminer' })).toHaveAttribute('href', '/');
    expect(sent).toEqual({ passphrase: PHRASE });
    // the file is handed to the browser as a download, and is the server's age file
    expect(downloads).toHaveLength(1);
    expect(downloads[0]?.name).toBe('keyring.age');
    expect(await blobText(downloads[0]?.blob as Blob)).toBe(AGE_FILE);
    // neither the phrase nor the file stays on the page, nor in the browser's storage
    expect(container.innerHTML).not.toContain(PHRASE);
    expect(container.innerHTML).not.toContain('BEGIN AGE');
    expect((screen.getByLabelText('Phrase de passe du trousseau') as HTMLInputElement).value).toBe('');
    expect(JSON.stringify({ ...window.localStorage, ...window.sessionStorage })).not.toContain(PHRASE);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:keyring');
  });

  // req: EF-UX-001, ENF-SEC-005
  it('test_finish_follows_what_the_server_says_not_what_the_screen_did', async () => {
    server.use(
      stateOf(() => ({ keyring_exported: false, jurisdiction: { country: 'FR', confirmed: true } })),
      http.post(`${BASE}/api/v1/setup/keyring-export`, () => new HttpResponse(AGE_FILE))
    );
    render(Page);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Phrase de passe du trousseau'), PHRASE);
    await user.click(screen.getByRole('button', { name: 'Exporter le trousseau' }));
    await screen.findByText('Trousseau exporté.');
    expect(screen.queryByRole('link', { name: 'Terminer' })).toBeNull(); // the server did not record it
  });

  // req: EF-UX-001, ENF-SEC-005, EF-USR-024
  it('test_refused_keyring_export_downloads_nothing_and_does_not_finish', async () => {
    server.use(
      stateOf(),
      http.post(`${BASE}/api/v1/setup/keyring-export`, () =>
        problem(403, { code: 'forbidden', title: 'Action refusée.' })
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Phrase de passe du trousseau'), PHRASE);
    await user.click(screen.getByRole('button', { name: 'Exporter le trousseau' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Action refusée.');
    expect(downloads).toHaveLength(0);
    expect(screen.queryByRole('link', { name: 'Terminer' })).toBeNull();
  });

  // req: EF-UX-001, EF-USR-022, ENF-SEC-005
  it('test_a_stale_session_asks_for_the_password_then_exports_again_with_the_same_phrase', async () => {
    let tries = 0;
    server.use(
      stateOf(),
      http.post(`${BASE}/api/v1/setup/keyring-export`, () => {
        tries += 1;
        return tries === 1
          ? problem(401, { code: 'reauth_required', reauth_methods: ['password'] })
          : new HttpResponse(AGE_FILE);
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, () =>
        HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' })
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Phrase de passe du trousseau'), PHRASE);
    await user.click(screen.getByRole('button', { name: 'Exporter le trousseau' }));
    await user.type(await screen.findByLabelText('Mot de passe'), 'portail');
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    expect(await screen.findByText('Trousseau exporté.')).toBeInTheDocument();
    expect(tries).toBe(2);
    expect(downloads).toHaveLength(1);
  });

  // req: EF-USR-023, EF-UX-001
  it('test_recovery_kit_is_the_printable_page_opened_by_its_route', async () => {
    server.use(stateOf());
    render(Page);
    const link = await screen.findByRole('link', { name: 'Imprimer le kit de secours' });
    expect(link).toHaveAttribute('href', '/api/v1/admin/recovery-kit');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link.getAttribute('rel')).toContain('noopener');
  });

  // req: EF-UX-001, EF-USR-001, EF-USR-024
  it('test_second_admin_is_offered_and_created_by_the_profiles_route', async () => {
    let body: unknown = null;
    server.use(
      stateOf(),
      http.post(`${BASE}/api/v1/profiles`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ id: 'U2', username: 'second', role: 'admin' }, { status: 201 });
      })
    );
    const { container } = render(Page);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Nom du second administrateur'), 'second');
    const password = screen.getByLabelText('Mot de passe du second administrateur');
    expect(password).toHaveAttribute('type', 'password');
    expect(password).toHaveAttribute('autocomplete', 'new-password');
    await user.type(password, 'un mot de passe long et nouveau');
    await user.click(screen.getByRole('button', { name: 'Ajouter le second administrateur' }));
    expect(await screen.findByText('Second administrateur ajouté.')).toBeInTheDocument();
    expect(body).toEqual({
      username: 'second',
      password: 'un mot de passe long et nouveau',
      role: 'admin'
    });
    expect(container.innerHTML).not.toContain('un mot de passe long');
    expect((screen.getByLabelText('Mot de passe du second administrateur') as HTMLInputElement).value).toBe('');
  });

  // req: EF-UX-001, EF-USR-001
  it('test_a_third_admin_is_refused_with_the_servers_problem', async () => {
    server.use(
      stateOf(),
      http.post(`${BASE}/api/v1/profiles`, () =>
        problem(409, { code: 'identity.too_many_admins', title: 'Deux administrateurs au plus.' })
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Nom du second administrateur'), 'third');
    await user.type(screen.getByLabelText('Mot de passe du second administrateur'), 'un mot de passe long');
    await user.click(screen.getByRole('button', { name: 'Ajouter le second administrateur' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Deux administrateurs au plus.');
    expect(screen.queryByText('Second administrateur ajouté.')).toBeNull();
  });

  // req: EF-UX-001, EF-SRC-029
  it('test_first_book_is_launched_from_a_typed_gutenberg_number', async () => {
    let body: unknown = null;
    server.use(
      stateOf(),
      http.post(`${BASE}/api/v1/acquire`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ job_id: 'J1' }, { status: 202 });
      })
    );
    render(Page);
    const user = userEvent.setup();
    const button = await screen.findByRole('button', { name: 'Lancer le premier livre' });
    expect(button).toBeDisabled(); // nothing typed
    await user.type(screen.getByLabelText('Numéro du livre sur Gutenberg (libre de droits aux États-Unis)'), ' 84 ');
    await user.click(button);
    expect(await screen.findByText('Premier livre lancé.')).toBeInTheDocument();
    expect(body).toEqual({ source_key: 'gutenberg', remote_id: '84' });
  });

  // req: EF-UX-001
  it('test_wizard_first_book_in_six_actions', async () => {
    // an "action" is one click or one typed field, from the first click of part 2 to the book
    let actions = 0;
    server.use(
      stateOf(() => ({
        shelves: [{ name: 'Manga', phrase: 'Les mangas vont dans Kavita › Manga', created: false }],
        sources: [
          { key: 'gutenberg', family: false },
          { key: 'wikisource', family: false }
        ],
        jurisdiction: { country: 'FR', confirmed: false }
      })),
      http.post(`${BASE}/api/v1/shelves/precreate`, () =>
        HttpResponse.json({ created: [] }, { status: 201 })
      ),
      http.put(`${BASE}/api/v1/setup/sources/family`, () => HttpResponse.json({ family: [] })),
      http.patch(`${BASE}/api/v1/config`, () => HttpResponse.json({})),
      http.post(`${BASE}/api/v1/acquire`, () => HttpResponse.json({ job_id: 'J1' }, { status: 202 }))
    );
    const user = userEvent.setup();
    const first = render(EtageresPage);
    await user.click(await screen.findByRole('button', { name: 'Créer les étagères' }));
    actions += 1;
    const next = await screen.findByRole('link', { name: 'Continuer' });
    actions += 1; // following the link
    expect(next).toHaveAttribute('href', '/assistant/fin');
    first.unmount();
    render(Page);
    await user.click(await screen.findByRole('button', { name: 'Confirmer la juridiction' }));
    actions += 1;
    await user.type(screen.getByLabelText('Numéro du livre sur Gutenberg (libre de droits aux États-Unis)'), '84');
    actions += 1;
    await user.click(screen.getByRole('button', { name: 'Lancer le premier livre' }));
    actions += 1;
    expect(await screen.findByText('Premier livre lancé.')).toBeInTheDocument();
    expect(actions).toBeLessThanOrEqual(6);
  });

  // req: EF-UX-001, EF-USR-024
  it('test_a_member_is_refused_by_the_server_and_sees_no_action', async () => {
    server.use(
      http.get(`${BASE}/api/v1/setup/state`, () =>
        problem(403, { code: 'forbidden', title: 'Action refusée.' })
      )
    );
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent('Action refusée.');
    expect(screen.queryByRole('button', { name: 'Exporter le trousseau' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Terminer' })).toBeNull();
  });
});
