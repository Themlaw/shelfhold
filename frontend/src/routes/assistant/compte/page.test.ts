import { render, screen } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();
const me = (role: string) =>
  HttpResponse.json({ id: 'U1', username: 'alice', role, nav: [], display_name: 'alice' });

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Assistant : compte', () => {
  // req: EF-UX-001
  it('test_account_screen_shows_the_admin_and_leads_to_kavita', async () => {
    server.use(http.get(`${BASE}/api/v1/me`, () => me('admin')));
    render(Page);
    expect(await screen.findByText('Connecté en tant que alice.')).toBeInTheDocument();
    expect(screen.getByText('Étape 1 sur 5 : Compte')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Continuer' })).toHaveAttribute('href', '/assistant/kavita');
  });

  // req: EF-UX-001, EF-USR-024
  it('test_account_screen_offers_nothing_to_a_member_or_a_child', async () => {
    for (const role of ['member', 'child']) {
      server.use(http.get(`${BASE}/api/v1/me`, () => me(role)));
      const { unmount } = render(Page);
      expect(await screen.findByRole('alert')).toHaveTextContent('Seul le compte administrateur');
      expect(screen.queryByRole('link', { name: 'Continuer' })).toBeNull();
      unmount();
    }
  });

  // req: EF-UX-001
  it('test_account_screen_sends_the_signed_out_to_the_sign_in_page', async () => {
    server.use(http.get(`${BASE}/api/v1/me`, () => HttpResponse.json({ status: 401 }, { status: 401 })));
    render(Page);
    expect(await screen.findByRole('link', { name: 'Se connecter' })).toHaveAttribute('href', '/login');
    expect(screen.queryByRole('link', { name: 'Continuer' })).toBeNull();
  });
});
