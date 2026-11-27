import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();
// no literal: gitleaks scans every commit
const NEW_PW = ['synthetic', 'profile', 'pw', '0047'].join('-');
const ADMIN_PW = ['synthetic', 'admin', 'pw', '0047'].join('-');
const profile = (id: string, username: string, role: string, extra: Record<string, unknown> = {}) => ({
  id, username, role, max_age_rating: null, can_rate: false, visibility: 'private',
  hide_unrated_remote: true, subject_filter: [], ...extra
});
const stale = () =>
  HttpResponse.json(
    {
      type: '/api/v1/problems/reauth-required', title: 'x', status: 401, code: 'reauth_required',
      auth_age_s: 2000, max_age_s: 900, reauth_methods: ['password']
    },
    { status: 401 }
  );
const forbidden = () =>
  HttpResponse.json(
    { type: '/api/v1/problems/forbidden', title: 'x', status: 403, code: 'forbidden' },
    { status: 403 }
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

let overview: Record<string, unknown> = { total_works: 0, steps: [], profiles: [] };
const list = (items: unknown[]) =>
  server.use(
    http.get(`${BASE}/api/v1/profiles`, () => HttpResponse.json({ items })),
    http.get(`${BASE}/api/v1/profiles/:id/source-policies`, () => HttpResponse.json({ items: [] })),
    http.get(`${BASE}/api/v1/profiles/overview`, () => HttpResponse.json(overview))
  );

describe('Profils', () => {
  // req: EF-USR-001
  it('lists the profiles and sends the password the admin typed, never one of its own', async () => {
    list([profile('p1', 'admin', 'admin')]);
    let body: Record<string, unknown> = {};
    server.use(
      http.post(`${BASE}/api/v1/profiles`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(profile('p2', 'Lea', 'child', { max_age_rating: 10 }), { status: 201 });
      })
    );
    render(Page);
    const user = userEvent.setup();
    expect(await screen.findByRole('button', { name: 'admin' })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Nouveau profil' }));
    await user.type(screen.getByLabelText('Nom'), 'Lea');
    await user.type(screen.getByLabelText('Mot de passe du profil'), NEW_PW);
    expect((screen.getByLabelText('Mot de passe du profil') as HTMLInputElement).type).toBe('password');
    await user.selectOptions(screen.getByLabelText('Rôle'), 'child');
    await user.type(screen.getByLabelText('Âge maximal'), '10');
    await user.click(screen.getByRole('button', { name: 'Créer' }));
    await screen.findByRole('status');
    expect(body).toEqual({
      username: 'Lea', password: NEW_PW, role: 'child', max_age_rating: 10,
      can_rate: false
    });
  });

  // req: EF-USR-022
  it('asks for the password in place when the session is not fresh, then creates the profile', async () => {
    list([profile('p1', 'admin', 'admin')]);
    let posts = 0;
    server.use(
      http.post(`${BASE}/api/v1/profiles`, () => {
        posts += 1;
        return posts === 1 ? stale() : HttpResponse.json(profile('p2', 'Lea', 'member'), { status: 201 });
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, () =>
        HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' })
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Nouveau profil' }));
    await user.type(await screen.findByLabelText('Nom'), 'Lea');
    await user.type(screen.getByLabelText('Mot de passe du profil'), NEW_PW);
    await user.click(screen.getByRole('button', { name: 'Créer' }));
    const dialog = await screen.findByLabelText('Mot de passe');
    await user.type(dialog, ADMIN_PW);
    await user.click(screen.getByRole('button', { name: /Confirmer/ }));
    await screen.findByText('Profil créé.');
    expect(posts).toBe(2);
  });

  // req: EF-USR-001, ENF-SEC-010
  it('shows nothing of the management to a member: the server refused the list', async () => {
    server.use(http.get(`${BASE}/api/v1/profiles`, forbidden));
    render(Page);
    expect(await screen.findByText(/réservés à l'administrateur/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Créer' })).toBeNull();
    expect(screen.queryByLabelText('Nom')).toBeNull();
  });

  // req: EF-USR-016
  it('test_visibility_setting_ui: changes the visibility of a profile through the server', async () => {
    list([profile('p2', 'Lea', 'member')]);
    let put: { url: string; body: unknown } | null = null;
    server.use(
      http.put(`${BASE}/api/v1/profiles/p2/visibility`, async ({ request }) => {
        put = { url: new URL(request.url).pathname, body: await request.json() };
        return HttpResponse.json({ visibility: 'household' });
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Lea' }));
    const li = await screen.findByTestId('profile-editor');
    await user.selectOptions(within(li).getByLabelText('Visibilité'), 'household');
    await screen.findByText('Enregistré.');
    expect(put).toEqual({ url: '/api/v1/profiles/p2/visibility', body: { visibility: 'household' } });
  });

  // req: EF-USR-001
  it('changes the age ceiling and the rating right of a profile', async () => {
    list([profile('p3', 'Max', 'child', { max_age_rating: 7 })]);
    let patched: unknown = null;
    server.use(
      http.patch(`${BASE}/api/v1/profiles/p3`, async ({ request }) => {
        patched = await request.json();
        return HttpResponse.json(profile('p3', 'Max', 'child', { max_age_rating: 12 }));
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Max' }));
    const li = await screen.findByTestId('profile-editor');
    // the rule of ages: a ceiling of 7 has a mark of its own, and 12+ is one click away
    expect(within(li).getByRole('radio', { name: '7+' })).toBeChecked();
    await user.click(within(li).getByRole('radio', { name: '12+' }));
    await screen.findByText('Enregistré.');
    expect(patched).toEqual({ max_age_rating: 12 });
  });

  // req: EF-USR-001
  it('reads and sets the policy of a profile for a source', async () => {
    list([profile('p3', 'Max', 'child', { max_age_rating: 7 })]);
    let put: unknown = null;
    server.use(
      http.get(`${BASE}/api/v1/profiles/p3/source-policies`, () =>
        HttpResponse.json({ items: [{ source_key: 'gutenberg', policy: 'request', effective: 'request' }] })
      ),
      http.put(`${BASE}/api/v1/profiles/p3/source-policies/gutenberg`, async ({ request }) => {
        put = await request.json();
        return HttpResponse.json({ source_key: 'gutenberg', policy: 'forbidden', effective: 'forbidden' });
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Max' }));
    const li = await screen.findByTestId('profile-editor');
    expect(await within(li).findByText('Politique : gutenberg')).toBeTruthy();
    expect(within(li).getByRole('radio', { name: 'Demander' })).toBeChecked();
    await user.click(within(li).getByRole('radio', { name: 'Interdit' }));
    await screen.findByText('Enregistré.');
    expect(put).toEqual({ policy: 'forbidden' });
  });

  // req: EF-USR-001
  it('removes a profile after the server accepted', async () => {
    list([profile('p2', 'Lea', 'member')]);
    let deleted = false;
    server.use(
      http.delete(`${BASE}/api/v1/profiles/p2`, () => {
        deleted = true;
        return new HttpResponse(null, { status: 204 });
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Lea' }));
    const li = await screen.findByTestId('profile-editor');
    await user.click(within(li).getByRole('button', { name: 'Supprimer' }));
    await user.click(await screen.findByRole('button', { name: 'Supprimer le profil' }));
    await screen.findByText('Profil supprimé.');
    expect(deleted).toBe(true);
  });

  // req: EF-USR-001
  it('keeps one editor open at a time and one danger zone, for the chosen profile', async () => {
    list([profile('p1', 'admin', 'admin'), profile('p2', 'Lea', 'member'), profile('p3', 'Max', 'child', { max_age_rating: 7 })]);
    render(Page);
    const user = userEvent.setup();
    const editor = await screen.findByTestId('profile-editor');
    expect(within(editor).getByRole('heading', { name: 'admin' })).toBeTruthy();
    expect(screen.getAllByText('Zone dangereuse')).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: 'Max' }));
    expect(within(screen.getByTestId('profile-editor')).getByRole('heading', { name: 'Max' })).toBeTruthy();
    expect(screen.getAllByTestId('profile-editor')).toHaveLength(1);
    expect(screen.getAllByText('Zone dangereuse')).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: 'Nouveau profil' }));
    expect(screen.queryByTestId('profile-editor')).toBeNull();
    expect(screen.getByLabelText('Nom')).toBeTruthy();
  });

  // req: EF-USR-001
  it('prints the works per mark of the age rule, and the last reading and e-readers of each profile', async () => {
    overview = {
      total_works: 120,
      steps: [{ ceiling: 0, works: 4 }, { ceiling: 10, works: 30 }, { ceiling: 12, works: 55 }, { ceiling: 13, works: 70 }, { ceiling: 16, works: 90 }, { ceiling: 18, works: 110 }],
      profiles: [
        { profile_id: 'p1', username: 'admin', role: 'admin', max_age_rating: null, works: 120, last_read: { at: '2026-11-20T10:00:00Z', work_id: 'w1', edition_id: 'e1', title: 'Une série d’essai' }, devices: [{ id: 'd1', label: 'Liseuse du salon', kind: 'kobo', last_seen_at: null }] },
        { profile_id: 'p2', username: 'Lea', role: 'child', max_age_rating: 10, works: 30, last_read: null, devices: [] }
      ]
    };
    list([profile('p1', 'admin', 'admin'), profile('p2', 'Lea', 'child', { max_age_rating: 10 })]);
    render(Page);
    const user = userEvent.setup();
    const register = await screen.findByRole('table');
    expect(await within(register).findByText('Une série d’essai')).toBeTruthy();
    expect(within(register).getByText('Liseuse du salon')).toBeTruthy();
    // a profile that read nothing and has no e-reader: nothing is written for it
    const lea = within(register).getByRole('button', { name: 'Lea' }).closest('tr') as HTMLElement;
    expect(within(lea).queryByText('Une série d’essai')).toBeNull();
    await user.click(within(register).getByRole('button', { name: 'Lea' }));
    const scale = await screen.findByRole('group', { name: 'Âge maximal' });
    expect(within(scale).getByText('30')).toBeTruthy();
    expect(within(scale).getByText('120')).toBeTruthy();
  });

  // req: EF-USR-001
  it('keeps the screen when the overview cannot be read: no figure, no error', async () => {
    list([profile('p1', 'Lea', 'child', { max_age_rating: 10 })]);
    server.use(http.get(`${BASE}/api/v1/profiles/overview`, () => HttpResponse.json({ title: 'x', status: 500, code: 'internal' }, { status: 500 })));
    render(Page);
    const scale = await screen.findByRole('group', { name: 'Âge maximal' });
    expect(within(scale).getAllByRole('radio').length).toBeGreaterThan(0);
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
