import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

vi.mock('#lib/passkeys.ts', async (original) => ({
  ...(await original<typeof import('#lib/passkeys.ts')>()),
  passkeysSupported: () => true,
  createPasskey: async () => ({ id: 'cred' })
}));

const BASE = 'http://shelfhold.test';
const server = setupServer();
const stale = () =>
  HttpResponse.json(
    {
      type: '/api/v1/problems/reauth-required', title: 'x', status: 401, code: 'reauth_required',
      auth_age_s: 2000, max_age_s: 900, reauth_methods: ['password']
    },
    { status: 401 }
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

describe('Clés d\'accès', () => {
  // req: EF-USR-022
  it('asks for the password in place when adding a passkey, then adds it', async () => {
    let options = 0;
    let registered = 0;
    server.use(
      http.get(`${BASE}/api/v1/auth/passkeys`, () =>
        HttpResponse.json({ available: true, reason: null, items: [] })
      ),
      http.post(`${BASE}/api/v1/auth/passkeys/options`, () => {
        options += 1;
        return options === 1 ? stale() : HttpResponse.json({ flow: 'f', options: {} });
      }),
      http.post(`${BASE}/api/v1/auth/passkeys`, () => {
        registered += 1;
        return HttpResponse.json({ id: 'p1', name: 'Laptop' }, { status: 201 });
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, () =>
        HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' })
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/^Nom de l/), 'Laptop');
    await user.click(screen.getByRole('button', { name: /Ajouter/ }));
    expect(registered).toBe(0); // nothing is added before the password
    await user.type(await screen.findByLabelText('Mot de passe'), 'secret');
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    await vi.waitFor(() => expect(registered).toBe(1));
    expect(options).toBe(2);
  });
});
