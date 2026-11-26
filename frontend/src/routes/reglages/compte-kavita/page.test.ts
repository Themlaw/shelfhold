import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import Page from './+page.svelte';

// The Kavita account of a member (EF-USR-012, D8, L33a): the key of their own Kavita account, per
// destination. The key goes in a field once, in the body of one request, and is never shown again:
// not in the page after the answer (a good one or a refusal), not in storage, not in what the
// server answers (it carries a fingerprint). `GET /api/v1/me`, `GET
// /api/v1/profiles/{id}/kavita-destinations`, `GET|PUT|DELETE .../destination-accounts`.
const BASE = 'http://shelfhold.test';
const server = setupServer();
const ME = '01JUSER000000000000000000M';
const DEST = '01JDEST000000000000000000A';
const OTHER = '01JDEST000000000000000000B';
// drawn here, never a literal that a secret scanner would flag
const KEY = ['kv', ...Array.from({ length: 3 }, () => Math.random().toString(36).slice(2, 10))].join('');
const ACCOUNTS = `${BASE}/api/v1/profiles/${ME}/destination-accounts`;
const choice = (over: Record<string, unknown> = {}) => ({
  id: DEST, shelf_name: 'Manga', target_name: 'Kavita maison', linked: false, ...over
});
const account = (over: Record<string, unknown> = {}) => ({
  id: '01JACCT000000000000000000A', user_id: ME, shelf_destination_id: DEST, remote_user: 'mum.kv',
  token_fingerprint: 'a1b2c3d4', verified_at: '2026-11-16T10:00:00Z', revoked_at: null, warnings: [], ...over
});
const problem = (code: string, status: number, extra: Record<string, unknown> = {}) =>
  HttpResponse.json(
    { type: `/api/v1/problems/${code}`, status, code, title: `title of ${code}`, cause: `cause of ${code}`, action: `action of ${code}`, ...extra },
    { status }
  );

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
  setUiLanguage('fr');
  localStorage.clear();
  sessionStorage.clear();
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const world = (choices: unknown[] = [choice()], role = 'member') =>
  server.use(
    http.get(`${BASE}/api/v1/me`, () => HttpResponse.json({ id: ME, username: 'mum', role, nav: [] })),
    http.get(`${BASE}/api/v1/profiles/${ME}/kavita-destinations`, () => HttpResponse.json({ items: choices })),
    http.get(`${BASE}/api/v1/sync-status`, () => HttpResponse.json({ items: [] })) // SyncStatus.test.ts
  );
const everywhere = () =>
  document.documentElement.outerHTML +
  JSON.stringify({ ...localStorage }) +
  JSON.stringify({ ...sessionStorage });
const card = async (shelf = 'Manga') =>
  (await screen.findByRole('heading', { name: new RegExp(shelf), level: 2 })).closest('section') as HTMLElement;

describe('Compte Kavita', () => {
  // req: EF-USR-012
  it('lists the Kavita destinations the member may link, with the shelf and the server', async () => {
    world([choice(), choice({ id: OTHER, shelf_name: 'Comics' })]);
    render(Page);
    expect(await screen.findByRole('heading', { name: 'Mon compte Kavita', level: 1 })).toBeTruthy();
    expect(await card('Manga')).toHaveTextContent('Kavita maison');
    expect(await card('Comics')).toBeTruthy();
    expect(screen.getAllByText('Pas de clé liée')).toHaveLength(2);
  });

  // req: EF-USR-012
  it('the key goes in a password field that the browser does not remember', async () => {
    world();
    render(Page);
    const field = within(await card()).getByLabelText('Clé de votre compte Kavita');
    expect(field).toHaveAttribute('type', 'password');
    expect(field).toHaveAttribute('autocomplete', 'off');
    expect(field).toHaveAttribute('spellcheck', 'false');
  });

  // req: EF-USR-012
  it('test_member_kavita_key_never_redisplayed: after a good link, nowhere in the page, the storage or the answers', async () => {
    world();
    let body: unknown = null;
    let answered = '';
    server.use(
      http.put(`${ACCOUNTS}/${DEST}`, async ({ request }) => {
        body = await request.json();
        answered = JSON.stringify(account());
        return HttpResponse.json(account());
      }),
      http.get(ACCOUNTS, () => HttpResponse.json({ items: [account()] }))
    );
    const user = userEvent.setup();
    render(Page);
    const manga = await card();
    await user.type(within(manga).getByLabelText('Nom de votre compte Kavita'), 'mum.kv');
    await user.type(within(manga).getByLabelText('Clé de votre compte Kavita'), KEY);
    await user.click(within(manga).getByRole('button', { name: 'Lier la clé' }));
    expect(await within(manga).findByText('Clé liée', { exact: false })).toBeTruthy();
    expect(body).toEqual({ token: KEY, remote_user: 'mum.kv' }); // once, in the body of one request
    expect(within(manga).getByText('a1b2c3d4')).toBeTruthy(); // the fingerprint says "this key"
    expect(within(manga).getByLabelText('Clé de votre compte Kavita')).toHaveValue('');
    expect(everywhere()).not.toContain(KEY);
    expect(answered).not.toContain(KEY);
    expect(screen.queryByDisplayValue(KEY)).toBeNull();
  });

  // req: EF-USR-012
  it('a refused key is cleared too, and the reason of the refusal is shown', async () => {
    world();
    server.use(http.put(`${ACCOUNTS}/${DEST}`, () => problem('destination_account_refused', 422, { reason: 'token_refused' })));
    const user = userEvent.setup();
    render(Page);
    const manga = await card();
    await user.type(within(manga).getByLabelText('Nom de votre compte Kavita'), 'mum.kv');
    await user.type(within(manga).getByLabelText('Clé de votre compte Kavita'), KEY);
    await user.click(within(manga).getByRole('button', { name: 'Lier la clé' }));
    const alert = await within(manga).findByRole('alert');
    expect(alert).toHaveTextContent('title of destination_account_refused');
    expect(alert).toHaveTextContent('Kavita a refusé cette clé');
    expect(within(manga).getByLabelText('Clé de votre compte Kavita')).toHaveValue('');
    expect(everywhere()).not.toContain(KEY);
    expect(within(manga).getByLabelText('Nom de votre compte Kavita')).toHaveValue('mum.kv'); // not a secret
  });

  // req: EF-USR-012
  it('a linked destination shows its fingerprint and no key, and offers to replace or revoke', async () => {
    world([choice({ linked: true })]);
    server.use(http.get(ACCOUNTS, () => HttpResponse.json({ items: [account()] })));
    render(Page);
    const manga = await card();
    expect(await within(manga).findByText('a1b2c3d4')).toBeTruthy();
    expect(manga).toHaveTextContent('mum.kv');
    expect(within(manga).getByRole('button', { name: 'Remplacer la clé' })).toBeTruthy();
    expect(within(manga).getByRole('button', { name: 'Révoquer la clé' })).toBeTruthy();
    expect(within(manga).getByLabelText('Clé de votre compte Kavita')).toHaveValue('');
  });

  // req: EF-USR-012
  it('revokes a key, and the card goes back to "no key"', async () => {
    world([choice({ linked: true })]);
    let listed = 0;
    server.use(
      http.get(ACCOUNTS, () => {
        listed += 1;
        return HttpResponse.json({ items: listed === 1 ? [account()] : [account({ remote_user: null, token_fingerprint: null, revoked_at: '2026-11-16T11:00:00Z' })] });
      }),
      http.delete(`${ACCOUNTS}/${DEST}`, () => new HttpResponse(null, { status: 204 }))
    );
    const user = userEvent.setup();
    render(Page);
    const manga = await card();
    await user.click(await within(manga).findByRole('button', { name: 'Révoquer la clé' }));
    expect(await within(manga).findByText('Pas de clé liée')).toBeTruthy();
    expect(within(manga).queryByText('a1b2c3d4')).toBeNull();
  });

  // req: EF-USR-022
  it('asks for a fresh session in place and replays the link, the key kept only for that call', async () => {
    world();
    let calls = 0;
    server.use(
      http.put(`${ACCOUNTS}/${DEST}`, () => {
        calls += 1;
        return calls === 1 ? problem('reauth_required', 401, { reauth_methods: ['password'] }) : HttpResponse.json(account());
      }),
      http.get(ACCOUNTS, () => HttpResponse.json({ items: [account()] })),
      http.post(`${BASE}/api/v1/auth/reauth`, () => HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' }))
    );
    const user = userEvent.setup();
    render(Page);
    const manga = await card();
    await user.type(within(manga).getByLabelText('Nom de votre compte Kavita'), 'mum.kv');
    await user.type(within(manga).getByLabelText('Clé de votre compte Kavita'), KEY);
    await user.click(within(manga).getByRole('button', { name: 'Lier la clé' }));
    const prompt = await screen.findByRole('region', { name: 'Confirmez votre identité' });
    await user.type(within(prompt).getByLabelText('Mot de passe'), 'x'.repeat(12));
    await user.click(within(prompt).getByRole('button', { name: 'Confirmer' }));
    expect(await within(manga).findByText('Clé liée', { exact: false })).toBeTruthy();
    expect(calls).toBe(2);
    expect(everywhere()).not.toContain(KEY);
  });

  // req: EF-USR-012
  it('a child is told that a parent enters their key, and no field is offered', async () => {
    server.use(
      http.get(`${BASE}/api/v1/me`, () => HttpResponse.json({ id: ME, username: 'kid', role: 'child', nav: [] }))
    );
    render(Page);
    expect(await screen.findByText('Un parent saisit la clé de ton compte Kavita.')).toBeTruthy();
    expect(screen.queryByLabelText('Clé de votre compte Kavita')).toBeNull();
  });

  // req: EF-USR-012
  it('the screen is in English when the interface is', async () => {
    setUiLanguage('en');
    world();
    render(Page);
    expect(await screen.findByRole('heading', { name: 'My Kavita account', level: 1 })).toBeTruthy();
    expect(within(await card()).getByLabelText('Key of your Kavita account')).toBeTruthy();
  });
});
