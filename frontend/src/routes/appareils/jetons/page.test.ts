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
    http.get(`${BASE}/api/v1/devices`, () => HttpResponse.json({ items: listed } as never)),
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
    {
      public_id: 'abcd2345',
      secret: SECRET,
      subject_user_id: 'm1',
      expires_at: null,
      device_id: 'd1'
    },
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

  const DEVICE = {
    id: 'd1',
    label: 'Salon',
    kind: 'koreader',
    subject_user_id: 'm1',
    subject_username: 'mum',
    public_id: 'abcd2345',
    created_at: '2026-11-16T00:00:00Z',
    last_seen_at: '2026-11-17T08:30:00Z',
    expires_at: null,
    revoked_at: null
  };

  // req: EF-USR-008, EF-UX-004
  it('lists the devices with their label, kind, profile and when they were last seen', async () => {
    listed = [DEVICE, { ...DEVICE, id: 'd2', label: 'Chevet', kind: 'kobo', last_seen_at: null }];
    serve('member');
    render(Page);
    const salon = (await screen.findByText(/Salon/)).closest('li') as HTMLElement;
    expect(salon).toHaveTextContent('KOReader');
    expect(salon).toHaveTextContent('mum');
    expect(salon).toHaveTextContent(/Vu pour la dernière fois/);
    const chevet = screen.getByText(/Chevet/).closest('li') as HTMLElement;
    expect(chevet).toHaveTextContent('Kobo');
    expect(chevet).toHaveTextContent(/Jamais vu/);
  });

  // req: EF-USR-008, ENF-SEC-013
  it('sends the kind chosen at pairing', async () => {
    serve('member');
    let sent: Record<string, unknown> = {};
    server.use(
      http.post(`${BASE}/api/v1/device-tokens`, async ({ request }) => {
        sent = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          { public_id: 'abcd2345', secret: SECRET, subject_user_id: 'm1', expires_at: null, device_id: 'd1' },
          { status: 201 }
        );
      })
    );
    render(Page);
    await userEvent.selectOptions(await screen.findByRole('combobox', { name: /Type d'appareil/ }), 'kobo');
    await userEvent.click(screen.getByRole('button', { name: 'Créer le jeton' }));
    await screen.findByTestId('secret');
    expect(sent.kind).toBe('kobo');
  });

  // req: EF-USR-008, ENF-SEC-013
  it('rotates the secret of a device: the new one is shown once and nothing is paired again', async () => {
    listed = [DEVICE];
    serve('member');
    let rotated = '';
    server.use(
      http.post(`${BASE}/api/v1/devices/d1/rotate`, () => {
        rotated = 'd1';
        return HttpResponse.json({
          public_id: 'abcd2345',
          secret: SECRET,
          subject_user_id: 'm1',
          expires_at: null,
          device_id: 'd1'
        });
      })
    );
    const { container } = render(Page);
    await userEvent.click(await screen.findByRole('button', { name: /Nouveau secret/ }));
    await userEvent.click(await screen.findByRole('button', { name: 'Créer un nouveau secret' }));
    await waitFor(() => expect(rotated).toBe('d1'));
    expect((await screen.findByTestId('secret')).textContent).toBe(SECRET);
    expect(screen.getByTestId('public-id').textContent).toBe('abcd2345');
    expect(window.location.href).not.toContain(SECRET);
    expect(JSON.stringify({ ...localStorage })).not.toContain(SECRET);
    await userEvent.click(screen.getByRole('button', { name: "J'ai noté le secret" }));
    expect(container.innerHTML).not.toContain(SECRET);
  });

  // req: EF-USR-008
  it('removes a device from the list', async () => {
    listed = [DEVICE];
    serve('member');
    let removed = false;
    server.use(
      http.delete(`${BASE}/api/v1/devices/d1`, () => {
        removed = true;
        listed = [];
        return new HttpResponse(null, { status: 204 });
      })
    );
    render(Page);
    await userEvent.click(await screen.findByRole('button', { name: 'Retirer' }));
    const confirm = await screen.findAllByRole('button', { name: 'Retirer' });
    await userEvent.click(confirm[confirm.length - 1]!);
    await waitFor(() => expect(removed).toBe(true));
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
