import { createRawSnippet } from 'svelte';
import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { signOut } from '#lib/session.ts';
import { STORAGE_KEY } from '#lib/themes/index.ts';
import Layout from './+layout.svelte';

const wiped = vi.hoisted(() => ({ calls: [] as string[] }));
vi.mock('#lib/offline/runtime.ts', () => ({
  wipeOfflineCopies: async () => void wiped.calls.push('wipe'),
  startOfflineSync: () => () => undefined,
  guardOfflineSession: async () => undefined
}));

const BASE = 'http://shelfhold.test';
const SOURCE = 'https://github.com/Themlaw/shelfhold/tree/v1.0.0';
const server = setupServer();

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const MEMBER = ['home', 'search', 'requests'];
const ADMIN = ['home', 'search', 'requests', 'approve', 'activity', 'settings'];
const screenOf = createRawSnippet(() => ({ render: () => '<main><h1>Un écran</h1></main>' }));

function shell(nav: string[], user: { name: string; role: string } | null, sourceUrl: string | null = SOURCE) {
  return render(Layout, {
    children: screenOf,
    data: { sourceUrl, appearance: null, nav, language: 'fr', user }
  } as never);
}

/** An item of an open menu. Floating menus are positioned after a measure jsdom cannot make: they
 * stay `visibility: hidden` here, where their items have no accessible name; found by their text. */
async function menuItem(role: 'menuitem' | 'menuitemradio', name: string): Promise<HTMLElement> {
  return vi.waitFor(() => {
    const item = screen.queryAllByRole(role, { hidden: true }).find((e) => e.textContent?.trim() === name);
    if (!item) throw new Error(`no ${role} ${name}`);
    return item;
  });
}

/** The entries of the table of contents (the side navigation), by group. */
function contents() {
  const toc = screen.getByRole('navigation', { name: 'Navigation principale' });
  return within(toc).getAllByRole('link').map((a) => a.textContent?.replace(/\s+/g, ' ').trim());
}

describe('table of contents by role (EF-UX-005)', () => {
  // req: EF-UX-005
  it('test_nav_by_role: a member reads, asks and keeps their account, and has no admin entry', () => {
    shell(MEMBER, { name: 'Antoine', role: 'member' });
    expect(contents()).toEqual([
      'Accueil',
      'Bibliothèque',
      'Sélections',
      'Collections',
      'Chercher',
      'Mes demandes',
      'Copies hors ligne',
      'Appareils et liseuses',
      'Clés d’accès',
      'Suivi de lecture'
    ]);
    expect(screen.queryByRole('link', { name: 'Activité' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Réglages' })).toBeNull();
  });

  // req: EF-UX-005
  it('an admin also has the family and the instance: approve, profiles, activity, health, settings, modules', () => {
    shell(ADMIN, { name: 'Claire', role: 'admin' });
    expect(contents()).toEqual([
      'Accueil',
      'Bibliothèque',
      'Sélections',
      'Collections',
      'Chercher',
      'Mes demandes',
      'Copies hors ligne',
      'À approuver',
      'Profils',
      'Activité',
      'Santé',
      'Réglages',
      'Modules',
      'Appareils et liseuses',
      'Clés d’accès',
      'Suivi de lecture'
    ]);
  });

  // req: EF-UX-005
  it('a child reads and asks: no selections, collections, devices nor account pages', () => {
    shell(MEMBER, { name: 'Louise', role: 'child' });
    expect(contents()).toEqual(['Accueil', 'Bibliothèque', 'Chercher', 'Mes demandes', 'Copies hors ligne']);
  });

  // req: EF-UX-005
  it('marks the entry of the screen shown, and the tab bar has four places and « Plus »', () => {
    window.history.replaceState(null, '', '/bibliotheque/selections');
    shell(MEMBER, { name: 'Antoine', role: 'member' });
    const toc = screen.getByRole('navigation', { name: 'Navigation principale' });
    expect(within(toc).getByRole('link', { name: 'Sélections' })).toHaveAttribute('aria-current', 'page');
    expect(within(toc).getByRole('link', { name: 'Bibliothèque' })).not.toHaveAttribute('aria-current');
    const bar = screen.getByRole('navigation', { name: 'Navigation principale (mobile)' });
    expect(within(bar).getAllByRole('link').map((a) => a.textContent?.trim())).toEqual([
      'Accueil',
      'Bibliothèque',
      'Chercher',
      'Mes demandes'
    ]);
    expect(within(bar).getByRole('button', { name: 'Plus' })).toBeInTheDocument();
    window.history.replaceState(null, '', '/');
  });

  // req: EF-UX-005
  it('nobody signed in: no navigation at all, a link to the sign-in page of the server', () => {
    shell([], null);
    expect(screen.queryByRole('navigation')).toBeNull();
    const signIn = screen.getByRole('link', { name: 'Se connecter' });
    expect(signIn).toHaveAttribute('href', '/login');
    expect(signIn).toHaveAttribute('data-sveltekit-reload'); // a page of the server, never the router
  });

  // req: ENF-A11Y-001
  it('offers a skip link to the screen, and the screen is rendered once', () => {
    shell(MEMBER, { name: 'Antoine', role: 'member' });
    expect(screen.getByRole('link', { name: 'Aller au contenu' })).toHaveAttribute('href', '#contenu');
    expect(document.getElementById('contenu')).toContainElement(screen.getByRole('heading', { level: 1 }));
    expect(screen.getAllByRole('main')).toHaveLength(1);
  });
});

describe('colophon with the source link (AGPL section 13)', () => {
  // req: ENF-LEG-012
  it('test_ui_footer_has_source_link: the colophon of every screen links to the source', () => {
    shell(MEMBER, { name: 'Antoine', role: 'member' });
    const footer = screen.getByRole('contentinfo');
    const link = within(footer).getByRole('link', { name: 'Code source' });
    expect(link).toHaveAttribute('href', SOURCE);
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'));
  });

  // req: ENF-LEG-012
  it('signed out too, and with an empty screen', () => {
    const empty = createRawSnippet(() => ({ render: () => '<span></span>' }));
    render(Layout, { children: empty, data: { sourceUrl: SOURCE, appearance: null, nav: [], language: 'fr', user: null } } as never);
    expect(within(screen.getByRole('contentinfo')).getByRole('link', { name: 'Code source' })).toBeInTheDocument();
  });

  // req: ENF-LEG-012, ENF-SEC-004
  it.each([
    'javascript:alert(1)',
    'JaVaScRiPt:alert(1)',
    ' javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'vbscript:x',
    '//evil.example/x',
    '/relative',
    ''
  ])('never turns %j into a link', (url) => {
    shell([], null, url);
    const footer = screen.getByRole('contentinfo');
    expect(within(footer).queryByRole('link', { name: 'Code source' })).toBeNull();
    expect(within(footer).getByText('Code source : adresse indisponible')).toBeInTheDocument();
    for (const link of within(footer).queryAllByRole('link')) {
      expect(link.getAttribute('href')).not.toContain(url.trim() || '#');
    }
  });
});

describe('appearance chosen from the masthead (EF-UX-012)', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-color-scheme');
    document.cookie = 'csrf=tok123';
  });

  // req: EF-UX-012
  it('applies, remembers on the device and keeps on the profile the scheme chosen', async () => {
    let body: unknown = null;
    server.use(
      http.patch(`${BASE}/api/v1/me/preferences`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ theme: 'bibliotheque', chosen_theme: null, color_scheme: 'dark', language: 'fr' });
      })
    );
    const user = userEvent.setup();
    shell(MEMBER, { name: 'Antoine', role: 'member' });
    await user.click(screen.getByRole('button', { name: /Apparence/ }));
    await user.click(await menuItem('menuitemradio', 'Sombre'));
    expect(document.documentElement.dataset.colorScheme).toBe('dark');
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}')).toMatchObject({ colorScheme: 'dark' });
    await vi.waitFor(() => expect(body).toEqual({ color_scheme: 'dark' }));
  });

  // req: EF-UX-012
  it('signed out, the choice stays on the device and nothing is sent', async () => {
    const user = userEvent.setup();
    shell([], null);
    await user.click(screen.getByRole('button', { name: /Apparence/ }));
    await user.click(await menuItem('menuitemradio', 'Clair'));
    expect(document.documentElement.dataset.colorScheme).toBe('light');
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}')).toMatchObject({ colorScheme: 'light' });
  });
});

describe('sign out (EF-USR-021)', () => {
  beforeEach(() => {
    wiped.calls = [];
    document.cookie = 'csrf=tok123';
  });

  // req: EF-USR-022
  it('the account menu of an admin, a member and a child offers to sign out', async () => {
    for (const role of ['admin', 'member', 'child']) {
      const user = userEvent.setup();
      const { unmount } = shell(role === 'admin' ? ADMIN : MEMBER, { name: 'Camille', role });
      await user.click(screen.getByRole('button', { name: /^Compte de Camille/ }));
      expect(await menuItem('menuitem', 'Se déconnecter')).toHaveAttribute('role', 'menuitem');
      await user.keyboard('{Escape}');
      unmount();
    }
  });

  // req: EF-USR-022
  it('calls the logout route with the CSRF token, then loads the sign-in page of the server', async () => {
    let csrf: string | null = null;
    server.use(
      http.post(`${BASE}/api/v1/auth/logout`, ({ request }) => {
        csrf = request.headers.get('x-csrf-token');
        return new HttpResponse(null, { status: 204 });
      })
    );
    const navigate = vi.fn();
    expect(await signOut(navigate)).toBe(true);
    expect(csrf).toBe('tok123');
    expect(navigate).toHaveBeenCalledWith('/login');
  });

  // req: EF-USR-022
  it('a network error does not claim the session is closed, and the shell says so', async () => {
    server.use(http.post(`${BASE}/api/v1/auth/logout`, () => HttpResponse.error()));
    const navigate = vi.fn();
    expect(await signOut(navigate)).toBe(false);
    expect(navigate).not.toHaveBeenCalled();
    const user = userEvent.setup();
    shell(MEMBER, { name: 'Antoine', role: 'member' });
    await user.click(screen.getByRole('button', { name: /^Compte de Antoine/ }));
    await user.click(await menuItem('menuitem', 'Se déconnecter'));
    expect(await screen.findByRole('alert')).toHaveTextContent('La déconnexion a échoué');
  });

  // req: EF-LEC-012, ENF-SEC-010
  it('logout wipes the profile offline copies, before the request and even if it fails', async () => {
    server.use(
      http.post(`${BASE}/api/v1/auth/logout`, () => {
        wiped.calls.push('request');
        return HttpResponse.error();
      })
    );
    await signOut(vi.fn());
    expect(wiped.calls).toEqual(['wipe', 'request']);
  });
});
