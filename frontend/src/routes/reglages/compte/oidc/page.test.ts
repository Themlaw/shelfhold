import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const ISSUER = 'https://idp.example.test';
const server = setupServer();

const linked = (byOther = false) =>
  http.get(`${BASE}/api/v1/auth/oidc/identities`, () =>
    HttpResponse.json({
      configured_issuer: ISSUER,
      items: [{ issuer: ISSUER, linked_at: '2026-11-01T10:00:00Z', linked_by_other: byOther }]
    })
  );

const reauthRequired = () =>
  HttpResponse.json(
    {
      type: 'https://shelfhold.invalid/problems/reauth-required',
      code: 'reauth_required',
      status: 401,
      reauth_methods: ['oidc', 'password']
    },
    { status: 401 }
  );

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
  sessionStorage.clear();
});
afterEach(() => {
  server.resetHandlers();
  vi.unstubAllGlobals();
});
afterAll(() => server.close());

describe('Identité du fournisseur (réglages du compte)', () => {
  // req: EF-USR-024
  it('shows the issuer, who made the link, and offers to unlink', async () => {
    server.use(linked(true));
    render(Page);
    expect(await screen.findByText(ISSUER)).toBeTruthy();
    expect(screen.getByText(/Liée par un administrateur/)).toBeTruthy();
    expect(screen.getByRole('button', { name: /Délier/ })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Lier mon identité/ })).toBeNull();
  });

  // req: EF-USR-024
  it('says when no provider is configured and offers nothing', async () => {
    server.use(
      http.get(`${BASE}/api/v1/auth/oidc/identities`, () =>
        HttpResponse.json({ configured_issuer: null, items: [] })
      )
    );
    render(Page);
    expect(await screen.findByText(/pas configuré/)).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });

  // req: EF-USR-024
  it('offers the provider first when the session was opened by it, and goes there', async () => {
    const assign = vi.fn();
    vi.stubGlobal('location', { ...window.location, assign, search: '' });
    let asked = 0;
    server.use(
      linked(),
      http.delete(`${BASE}/api/v1/auth/oidc/identities`, () => reauthRequired()),
      http.post(`${BASE}/api/v1/auth/oidc/reauth`, () => {
        asked += 1;
        return HttpResponse.json({
          authorization_url: `${ISSUER}/authorize?prompt=login&max_age=300`
        });
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /Délier/ }));
    await user.click(await screen.findByRole('button', { name: /fournisseur/i }));
    await vi.waitFor(() => expect(asked).toBe(1));
    expect(assign).toHaveBeenCalledWith(`${ISSUER}/authorize?prompt=login&max_age=300`);
    expect(sessionStorage.getItem('oidc-intent')).toBe('unlink'); // taken up on the way back
  });

  // req: EF-USR-024
  it('takes up the unlink after the provider confirmed the person', async () => {
    sessionStorage.setItem('oidc-intent', 'unlink');
    vi.stubGlobal('location', { ...window.location, search: '?oidc=reauth' });
    let deleted = 0;
    server.use(
      linked(),
      http.delete(`${BASE}/api/v1/auth/oidc/identities`, () => {
        deleted += 1;
        return new HttpResponse(null, { status: 204 });
      })
    );
    render(Page);
    await vi.waitFor(() => expect(deleted).toBe(1));
    expect(sessionStorage.getItem('oidc-intent')).toBeNull();
  });

  // req: EF-USR-024
  it('shows the result of a link or a refusal that comes back in the address', async () => {
    vi.stubGlobal('location', { ...window.location, search: '?oidc=linked' });
    server.use(linked());
    render(Page);
    expect(await screen.findByRole('status')).toBeTruthy();
    expect(screen.getByText('Identité liée.')).toBeTruthy();
  });

  // req: EF-USR-024
  it('does not take up an intent the provider did not confirm', async () => {
    sessionStorage.setItem('oidc-intent', 'unlink');
    vi.stubGlobal('location', { ...window.location, search: '?oidc=refused' });
    let deleted = 0;
    server.use(
      linked(),
      http.delete(`${BASE}/api/v1/auth/oidc/identities`, () => {
        deleted += 1;
        return new HttpResponse(null, { status: 204 });
      })
    );
    render(Page);
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(deleted).toBe(0);
    expect(sessionStorage.getItem('oidc-intent')).toBeNull();
  });
});
