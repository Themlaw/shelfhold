import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();

const SHELVES = [
  { name: 'Manga', phrase: 'Phrase du serveur › Manga', created: false },
  { name: 'Comics et BD', phrase: 'Phrase du serveur › Comics', created: false },
  { name: 'Livres', phrase: 'Phrase du serveur › Livres', created: false },
  { name: 'Famille', phrase: 'Phrase du serveur › Famille', created: false }
];
const SOURCES = [
  { key: 'gutenberg', family: false },
  { key: 'wikisource', family: false },
  { key: 'standardebooks', family: false }
];
const state = (over: Record<string, unknown> = {}) =>
  http.get(`${BASE}/api/v1/setup/state`, () =>
    HttpResponse.json({
      targets: [],
      destinations: [],
      first_run: false,
      shelves: SHELVES,
      sources: SOURCES,
      keyring_exported: false,
      jurisdiction: { country: 'FR', confirmed: false },
      ...over
    })
  );
const problem = (status: number, extra: Record<string, unknown> = {}) =>
  HttpResponse.json(
    { type: '/api/v1/problems/x', title: 'Refusé.', status, code: 'x', ...extra },
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

describe('Assistant : étagères et sources', () => {
  // req: EF-UX-007, EF-UX-001
  it('test_precreated_shelves_sentence_screen', async () => {
    server.use(state());
    const { container } = render(Page);
    for (const shelf of SHELVES) {
      expect(await screen.findByText(shelf.phrase)).toBeInTheDocument(); // the server's sentence
    }
    expect(container.querySelectorAll('[data-shelf]')).toHaveLength(4);
    expect(container.querySelector('details')?.open).toBe(false); // advanced options are folded
    expect(screen.queryByRole('link', { name: 'Continuer' })).toBeNull();
  });

  // req: EF-UX-007
  it('test_shelf_sentences_come_from_the_server_and_are_never_markup', async () => {
    const hostile = '<img src=x onerror=alert(1)>';
    server.use(state({ shelves: [{ name: 'Manga', phrase: hostile, created: false }] }));
    const { container } = render(Page);
    expect(await screen.findByText(hostile)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
  });

  // req: EF-UX-001, EF-USR-014
  it('test_creating_the_shelves_marks_gutenberg_and_wikisource_family_by_default', async () => {
    const calls: string[] = [];
    let precreate: unknown = null;
    let family: unknown = null;
    server.use(
      state(),
      http.post(`${BASE}/api/v1/shelves/precreate`, async ({ request }) => {
        calls.push('precreate');
        precreate = await request.json();
        return HttpResponse.json({ created: ['Manga'] }, { status: 201 });
      }),
      http.put(`${BASE}/api/v1/setup/sources/family`, async ({ request }) => {
        calls.push('family');
        family = await request.json();
        return HttpResponse.json({ family: ['gutenberg', 'wikisource'] });
      })
    );
    render(Page);
    const user = userEvent.setup();
    expect(await screen.findByRole('checkbox', { name: /Gutenberg/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /Wikisource/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /Standard Ebooks/ })).not.toBeChecked();
    await user.click(screen.getByRole('button', { name: 'Créer les étagères' }));
    expect(await screen.findByRole('link', { name: 'Continuer' })).toHaveAttribute(
      'href',
      '/assistant/fin'
    );
    expect(calls).toEqual(['precreate', 'family']);
    expect(precreate).toEqual({ adopted: [] });
    expect(family).toEqual({ source_keys: ['gutenberg', 'wikisource'] });
  });

  // req: EF-USR-014
  it('test_unchecking_a_source_leaves_it_out_of_the_family_mark', async () => {
    let family: { source_keys: string[] } | null = null;
    server.use(
      state(),
      http.post(`${BASE}/api/v1/shelves/precreate`, () =>
        HttpResponse.json({ created: [] }, { status: 201 })
      ),
      http.put(`${BASE}/api/v1/setup/sources/family`, async ({ request }) => {
        family = (await request.json()) as { source_keys: string[] };
        return HttpResponse.json({ family: family.source_keys });
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('checkbox', { name: /Wikisource/ }));
    await user.click(screen.getByRole('checkbox', { name: /Standard Ebooks/ }));
    await user.click(screen.getByRole('button', { name: 'Créer les étagères' }));
    await screen.findByRole('link', { name: 'Continuer' });
    expect(family).toEqual({ source_keys: ['gutenberg', 'standardebooks'] });
  });

  // req: EF-USR-014, EF-USR-024
  it('test_a_refused_family_mark_does_not_let_the_wizard_move_on', async () => {
    server.use(
      state(),
      http.post(`${BASE}/api/v1/shelves/precreate`, () =>
        HttpResponse.json({ created: [] }, { status: 201 })
      ),
      http.put(`${BASE}/api/v1/setup/sources/family`, () =>
        problem(403, { code: 'forbidden', title: 'Action refusée.' })
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Créer les étagères' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Action refusée.');
    expect(screen.queryByRole('link', { name: 'Continuer' })).toBeNull();
    expect(screen.queryByText('Étagères créées.')).toBeNull();
  });

  // req: EF-UX-001, EF-USR-022
  it('test_a_stale_session_asks_for_the_password_in_place_then_runs_both_again', async () => {
    let tries = 0;
    let marks = 0;
    server.use(
      state(),
      http.post(`${BASE}/api/v1/shelves/precreate`, () => {
        tries += 1;
        return tries === 1
          ? problem(401, { code: 'reauth_required', reauth_methods: ['password'] })
          : HttpResponse.json({ created: [] }, { status: 201 });
      }),
      http.put(`${BASE}/api/v1/setup/sources/family`, () => {
        marks += 1;
        return HttpResponse.json({ family: [] });
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, () =>
        HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' })
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Créer les étagères' }));
    await user.type(await screen.findByLabelText('Mot de passe'), 'portail');
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    expect(await screen.findByRole('link', { name: 'Continuer' })).toBeInTheDocument();
    expect(tries).toBe(2);
    expect(marks).toBe(1);
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
    expect(screen.queryByRole('button', { name: 'Créer les étagères' })).toBeNull();
  });
});
