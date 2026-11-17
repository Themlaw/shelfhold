import { render, screen, within } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import SyncStatus from './SyncStatus.svelte';

// The state of the progress sync, member by member (EF-EXP-026, L33b): `GET /api/v1/sync-status`.
// The server decides who is shown (a member: themself; an admin: everyone); the screen draws what
// it is given, says "never" for a missing date and names a member without a key.
const BASE = 'http://shelfhold.test';
const server = setupServer();
const row = (over: Record<string, unknown> = {}) => ({
  user_id: '01JUSER000000000000000000M', username: 'mum', shelf_destination_id: '01JDEST000000000000000000A',
  shelf_name: 'Manga', target_name: 'Kavita maison', state: 'linked',
  last_pull_at: '2026-11-15T11:00:00Z', last_push_at: '2026-11-15T11:05:00Z', ...over
});
const answer = (items: unknown[]) =>
  server.use(http.get(`${BASE}/api/v1/sync-status`, () => HttpResponse.json({ items })));

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => setUiLanguage('fr'));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('SyncStatus', () => {
  // req: EF-EXP-026
  it('shows the last pull and the last push of the member, per binding', async () => {
    answer([row()]);
    render(SyncStatus);
    const card = (await screen.findByRole('heading', { name: /Manga/, level: 4 })).closest('li') as HTMLElement;
    expect(card).toHaveTextContent('Kavita maison');
    expect(card).toHaveTextContent('Clé liée');
    expect(within(card).getByText(/Dernière lecture/)).toHaveTextContent(/2026|15/);
    expect(within(card).getByText(/Dernier envoi/)).toHaveTextContent(/2026|15/);
  });

  // req: EF-EXP-026
  it('says never for a date that is missing, and names a member without a token', async () => {
    answer([row({ state: 'no_token', last_pull_at: null, last_push_at: null })]);
    render(SyncStatus);
    const card = (await screen.findByRole('heading', { name: /Manga/, level: 4 })).closest('li') as HTMLElement;
    expect(card).toHaveTextContent('Pas de jeton lié');
    expect(within(card).getByText(/Dernière lecture/)).toHaveTextContent('jamais');
    expect(within(card).getByText(/Dernier envoi/)).toHaveTextContent('jamais');
  });

  // req: EF-EXP-026
  it('groups the rows of an admin by member', async () => {
    answer([
      row(),
      row({ user_id: '01JUSER000000000000000000D', username: 'dad', state: 'no_token', last_pull_at: null, last_push_at: null })
    ]);
    render(SyncStatus);
    expect(await screen.findByRole('heading', { name: 'mum', level: 3 })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'dad', level: 3 })).toBeTruthy();
  });

  // req: EF-EXP-026
  it('draws nothing when the server refuses (a child) and does not guess', async () => {
    server.use(
      http.get(`${BASE}/api/v1/sync-status`, () =>
        HttpResponse.json({ type: '/api/v1/problems/forbidden', status: 403, code: 'forbidden', title: 'x', cause: 'y', action: 'z' }, { status: 403 })
      )
    );
    const { container } = render(SyncStatus);
    await new Promise((r) => setTimeout(r, 50));
    expect(container.querySelector('section')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  // req: EF-EXP-026
  it('says there is nothing to show when there is no binding, in English too', async () => {
    setUiLanguage('en');
    answer([]);
    render(SyncStatus);
    expect(await screen.findByText(/No Kavita binding/)).toBeTruthy();
  });

  // req: EF-USR-012
  it('keeps no key and no fingerprint in the page: it only draws what it is given', async () => {
    answer([row()]);
    const { container } = render(SyncStatus);
    await screen.findByRole('heading', { name: /Manga/, level: 4 });
    expect(container.querySelectorAll('input')).toHaveLength(0);
    expect(container.innerHTML).not.toMatch(/fingerprint|token_|kv[a-z0-9]{12}/i);
  });
});
