import { render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();
// drawn at each run: no literal secret in the sources
const ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789';
const SECRET = Array.from(crypto.getRandomValues(new Uint8Array(26)), (b) => ALPHABET[b % ALPHABET.length]).join('');
const SETUP = {
  opds_path: '/opds',
  kosync_path: '/kosync',
  server_time: new Date().toISOString(),
  https: true,
  plain_http_allowed: false,
  document_matching: 'binary'
};
const ME = (role: string) => ({ id: 'u1', nav: [], role, username: 'x', locale: 'fr' });
const PROFILES = {
  items: [
    { id: 'a1', username: 'boss', role: 'admin' },
    { id: 'm1', username: 'mum', role: 'member' }
  ]
};
let listed: unknown[] = [];

function serve(role: string, extra: Parameters<typeof http.get>[1][] = []): void {
  server.use(
    http.get(`${BASE}/api/v1/me`, () => HttpResponse.json(ME(role) as never)),
    http.get(`${BASE}/api/v1/device-tokens/setup`, () => HttpResponse.json(SETUP)),
    http.get(`${BASE}/api/v1/device-tokens`, () => HttpResponse.json({ items: listed } as never)),
    http.get(`${BASE}/api/v1/profiles`, () => HttpResponse.json(PROFILES as never)),
    ...extra.map((handler) => http.get(`${BASE}/never`, handler))
  );
}

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => {
  server.resetHandlers();
  listed = [];
  localStorage.clear();
  sessionStorage.clear();
});
afterAll(() => server.close());

const created = http.post(`${BASE}/api/v1/device-tokens`, () =>
  HttpResponse.json(
    { public_id: 'abcd2345', secret: SECRET, subject_user_id: 'm1', expires_at: null },
    { status: 201 }
  )
);

describe('Connecter une liseuse', () => {
  // req: EF-UX-004
  it('writes the addresses, the server time and the warnings, and draws a code of the address only', async () => {
    serve('member');
    const { container } = render(Page);
    expect(await screen.findByRole('heading', { name: 'Connecter une liseuse' })).toBeInTheDocument();
    expect(await screen.findByText(`${window.location.origin}/opds`)).toBeInTheDocument();
    expect(screen.getByText(`${window.location.origin}/kosync`)).toBeInTheDocument();
    expect(screen.getByText(/Heure du serveur/)).toBeInTheDocument();
    expect(screen.getByText(/HTTPS est exigé/)).toBeInTheDocument();
    expect(screen.getByText(/« binaire »/)).toBeInTheDocument();
    expect(screen.getByText(/qu'un serveur de synchronisation/)).toBeInTheDocument();
    expect(screen.getByText(/aucune heure/)).toBeInTheDocument();
    const codes = [...container.querySelectorAll('svg[data-qr-text]')];
    expect(codes.map((c) => c.getAttribute('data-qr-text'))).toEqual([`${window.location.origin}/opds`]);
  });

  // req: EF-UX-004, ENF-SEC-010
  it('shows the secret once, as it is, and takes it out of the page when it is noted', async () => {
    serve('member');
    server.use(created);
    const { container } = render(Page);
    await userEvent.click(await screen.findByRole('button', { name: 'Créer le jeton' }));
    const secret = await screen.findByTestId('secret');
    expect(secret.textContent).toBe(SECRET); // no separator, nothing added
    expect(screen.getByText(/tel quel/)).toBeInTheDocument();
    for (const code of container.querySelectorAll('svg[data-qr-text]')) {
      expect(code.getAttribute('data-qr-text')).not.toContain(SECRET);
    }
    expect(window.location.href).not.toContain(SECRET);
    expect(JSON.stringify({ ...localStorage })).not.toContain(SECRET);
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(SECRET);
    await userEvent.click(screen.getByRole('button', { name: "J'ai noté le secret" }));
    expect(container.innerHTML).not.toContain(SECRET);
  });

  // req: EF-UX-004, ENF-SEC-010
  it('removes the secret from the page when the screen is left', async () => {
    serve('member');
    server.use(created);
    const { container, unmount } = render(Page);
    await userEvent.click(await screen.findByRole('button', { name: 'Créer le jeton' }));
    await screen.findByTestId('secret');
    const host = container;
    unmount();
    expect(host.innerHTML).not.toContain(SECRET);
    expect(document.body.innerHTML).not.toContain(SECRET);
  });

  // req: EF-UX-004
  it('lets an admin name a member or a child, never an admin, and a child nothing', async () => {
    serve('admin');
    const { unmount } = render(Page);
    const choice = await screen.findByRole('combobox', { name: /Profil lu/ });
    await waitFor(() => expect(choice.querySelectorAll('option')).toHaveLength(1));
    expect(choice).toHaveTextContent('mum');
    expect(choice).not.toHaveTextContent('boss');
    unmount();
    serve('child');
    render(Page);
    await screen.findByText(/Aucune liseuse n'est connectée/);
    expect(screen.queryByRole('button', { name: 'Créer le jeton' })).toBeNull();
  });

  // req: EF-UX-004
  it('revokes a token from the list', async () => {
    listed = [
      {
        public_id: 'abcd2345',
        name: 'Salon',
        subject_user_id: 'm1',
        subject_username: 'mum',
        created_at: '2026-11-16T00:00:00Z',
        expires_at: null,
        last_used_at: null
      }
    ];
    serve('member');
    let revoked = false;
    server.use(
      http.delete(`${BASE}/api/v1/device-tokens/abcd2345`, () => {
        revoked = true;
        listed = [];
        return new HttpResponse(null, { status: 204 });
      })
    );
    render(Page);
    await userEvent.click(await screen.findByRole('button', { name: 'Révoquer' }));
    await waitFor(() => expect(revoked).toBe(true));
    expect(await screen.findByText(/Aucune liseuse n'est connectée/)).toBeInTheDocument();
  });

  // req: EF-UX-004
  it('warns when the connection is not encrypted', async () => {
    serve('member');
    server.use(
      http.get(`${BASE}/api/v1/device-tokens/setup`, () =>
        HttpResponse.json({ ...SETUP, https: false })
      )
    );
    render(Page);
    expect(await screen.findByText(/servie en HTTP simple/)).toBeInTheDocument();
  });
});
