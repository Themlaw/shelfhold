import { render, screen } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();
const me = (role: string) =>
  HttpResponse.json({ id: 'U1', username: 'alice', role, nav: [], display_name: 'alice' });

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => {
  server.resetHandlers();
  // Reset to French after each test to avoid interference
  setUiLanguage('fr');
});
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

  // req: ENF-I18N-001
  it('renders all text in English when the browser is in English', async () => {
    // Set the UI language to English to simulate a browser with English as the preferred language
    setUiLanguage('en');
    server.use(http.get(`${BASE}/api/v1/me`, () => HttpResponse.json({ status: 401 }, { status: 401 })));
    render(Page);
    
    // Wait for async rendering
    expect(await screen.findByRole('link', { name: 'Sign in' })).toBeInTheDocument();
    
    // Verify that the title is in English
    expect(screen.getByText('First Launch Wizard')).toBeInTheDocument();
    expect(screen.getByText('Your account')).toBeInTheDocument();
    
    // Verify no French text is present
    expect(screen.queryByText('Assistant de premier lancement')).toBeNull();
    expect(screen.queryByText('Votre compte')).toBeNull();
    expect(screen.queryByText('Se connecter')).toBeNull();
  });

  // req: ENF-I18N-001
  it('renders all text in French when the browser is in French', async () => {
    // Set the UI language to French to simulate a browser with French as the preferred language
    setUiLanguage('fr');
    server.use(http.get(`${BASE}/api/v1/me`, () => HttpResponse.json({ status: 401 }, { status: 401 })));
    render(Page);
    
    // Wait for async rendering
    expect(await screen.findByRole('link', { name: 'Se connecter' })).toBeInTheDocument();
    
    // Verify that the title is in French
    expect(screen.getByText('Assistant de premier lancement')).toBeInTheDocument();
    expect(screen.getByText('Votre compte')).toBeInTheDocument();
    
    // Verify no English text is present
    expect(screen.queryByText('First Launch Wizard')).toBeNull();
    expect(screen.queryByText('Your account')).toBeNull();
    expect(screen.queryByText('Sign in')).toBeNull();
  });
});
