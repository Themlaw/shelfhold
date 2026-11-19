import { goto } from '$app/navigation';
import { createRawSnippet } from 'svelte';
import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Layout from '../../routes/+layout.svelte';
import Nav from './Nav.svelte';

vi.mock('$app/navigation', () => ({ goto: vi.fn() }));
const wiped = vi.hoisted(() => ({ calls: [] as string[] }));
vi.mock('#lib/offline/runtime.ts', () => ({
  wipeOfflineCopies: async () => void wiped.calls.push('wipe'),
  startOfflineSync: () => () => undefined
}));

const BASE = 'http://shelfhold.test';
const server = setupServer();

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const links = () => within(screen.getByRole('navigation')).getAllByRole('link').map((a) => a.textContent?.trim());

describe('Nav (EF-UX-005)', () => {
  // req: EF-UX-005
  it('test_nav_by_role: a member sees the family tabs and no admin tab', () => {
    render(Nav, { ids: ['home', 'search', 'requests'] });
    expect(links()).toEqual(['Accueil', 'Chercher', 'Mes demandes']);
    expect(screen.queryByRole('link', { name: 'Activité' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Réglages' })).toBeNull();
  });

  // req: EF-UX-005
  it('an admin also sees À approuver, Activité and Réglages', () => {
    render(Nav, { ids: ['home', 'search', 'requests', 'approve', 'activity', 'settings'] });
    expect(links()).toEqual(['Accueil', 'Chercher', 'Mes demandes', 'À approuver', 'Activité', 'Réglages']);
  });

  // req: EF-UX-005
  it('marks the current tab and renders nothing for nobody signed in', () => {
    const { unmount } = render(Nav, { ids: ['home', 'search'], current: '/chercher' });
    expect(screen.getByRole('link', { name: 'Chercher' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Accueil' })).not.toHaveAttribute('aria-current');
    unmount();
    render(Nav, { ids: [] });
    expect(screen.queryByRole('navigation')).toBeNull();
  });

  // req: EF-UX-005
  it('the root layout shows the tabs of its data, and none when nobody is signed in', () => {
    const page = createRawSnippet(() => ({ render: () => '<main></main>' }));
    const { unmount } = render(Layout, {
      children: page,
      data: { sourceUrl: null, appearance: null, nav: ['home', 'activity'] }
    } as never);
    expect(links()).toEqual(['Accueil', 'Activité']);
    unmount();
    render(Layout, { children: page, data: { sourceUrl: null, appearance: null, nav: [], language: 'fr' } } as never);
    expect(screen.queryByRole('navigation')).toBeNull();
  });
});

describe('Nav logout (EF-USR-021)', () => {
  beforeEach(() => {
    vi.mocked(goto).mockClear();
    document.cookie = 'csrf=tok123';
  });

  // req: EF-USR-022
  it('shows a logout button for an admin, a member and a child', () => {
    for (const ids of [
      ['home', 'search', 'requests', 'approve', 'activity', 'settings'],
      ['home', 'search', 'requests'],
      ['home', 'search', 'requests']
    ]) {
      const { unmount } = render(Nav, { ids });
      expect(screen.getByRole('button', { name: 'Se déconnecter' })).toBeInTheDocument();
      unmount();
    }
  });

  // req: EF-USR-022
  it('does not show a logout button when nobody is signed in', () => {
    render(Nav, { ids: [] });
    expect(screen.queryByRole('button', { name: 'Se déconnecter' })).toBeNull();
  });

  // req: EF-USR-022
  it('calls the logout route with the CSRF token then goes to /login', async () => {
    let called = false;
    let csrf: string | null = null;
    server.use(
      http.post(`${BASE}/api/v1/auth/logout`, ({ request }) => {
        called = true;
        csrf = request.headers.get('x-csrf-token');
        return new HttpResponse(null, { status: 204 });
      })
    );
    const user = userEvent.setup();
    render(Nav, { ids: ['home', 'search', 'requests'] });
    await user.click(screen.getByRole('button', { name: 'Se déconnecter' }));
    await vi.waitFor(() => expect(goto).toHaveBeenCalledWith('/login'));
    expect(called).toBe(true);
    expect(csrf).toBe('tok123');
  });

  // req: EF-USR-022
  it('shows a message on a network error and does not claim the session is closed', async () => {
    server.use(http.post(`${BASE}/api/v1/auth/logout`, () => HttpResponse.error()));
    const user = userEvent.setup();
    render(Nav, { ids: ['home', 'search', 'requests'] });
    await user.click(screen.getByRole('button', { name: 'Se déconnecter' }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(goto).not.toHaveBeenCalled();
  });
});

describe('Nav logout wipes the offline copies (M5 L16b)', () => {
  beforeEach(() => {
    wiped.calls = [];
    document.cookie = 'csrf=tok123';
  });

  // req: EF-LEC-012, ENF-SEC-010
  it('logout wipes the profile offline copies, before the request and even if it fails', async () => {
    server.use(
      http.post(`${BASE}/api/v1/auth/logout`, () => {
        wiped.calls.push('request');
        return HttpResponse.error();
      })
    );
    const user = userEvent.setup();
    render(Nav, { ids: ['home'] });
    await user.click(screen.getByRole('button', { name: 'Se déconnecter' }));
    await screen.findByRole('alert');
    expect(wiped.calls).toEqual(['wipe', 'request']);
  });
});
