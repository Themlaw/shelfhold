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

const list = (items: unknown[]) =>
  server.use(
    http.get(`${BASE}/api/v1/profiles`, () => HttpResponse.json({ items })),
    http.get(`${BASE}/api/v1/profiles/:id/source-policies`, () => HttpResponse.json({ items: [] }))
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
    expect(await screen.findByText(/admin — admin/)).toBeTruthy();
    await user.type(screen.getByLabelText('Nom'), 'Lea');
    await user.type(screen.getByLabelText('Mot de passe du profil'), NEW_PW);
    await user.selectOptions(screen.getByLabelText('Rôle'), 'child');
    await user.type(screen.getByLabelText('Âge maximal'), '10');
    await user.click(screen.getByRole('button', { name: 'Créer' }));
    await screen.findByRole('status');
    expect(body).toEqual({
      username: 'Lea', password: NEW_PW, role: 'child', max_age_rating: 10,
      can_rate: false
    });
    expect((screen.getByLabelText('Mot de passe du profil') as HTMLInputElement).type).toBe('password');
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
    expect((await screen.findByRole('alert')).textContent).toMatch(/réservés à l'administrateur/);
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
    const li = (await screen.findByText(/Lea — member/)).closest('li')!;
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
    const li = (await screen.findByText(/Max — child/)).closest('li')!;
    const age = within(li).getByLabelText('Âge maximal') as HTMLInputElement;
    await user.clear(age);
    await user.type(age, '12');
    await user.tab();
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
    const li = (await screen.findByText(/Max — child/)).closest('li')!;
    await user.selectOptions(await within(li).findByLabelText(/Politique : gutenberg/), 'forbidden');
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
    const li = (await screen.findByText(/Lea — member/)).closest('li')!;
    await user.click(within(li).getByRole('button', { name: 'Supprimer' }));
    await screen.findByText('Profil supprimé.');
    expect(deleted).toBe(true);
  });
});
