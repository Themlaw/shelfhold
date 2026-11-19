import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Identités du fournisseur', () => {
  // req: EF-USR-004
  it('shows the links of the account and who made them, never an identifier', async () => {
    server.use(
      http.get(`${BASE}/api/v1/auth/oidc/identities`, () =>
        HttpResponse.json({
          items: [
            { issuer: 'https://idp.example.test', linked_at: '2026-11-01T10:00:00Z', linked_by_other: true }
          ]
        })
      )
    );
    render(Page);
    expect(await screen.findByText('https://idp.example.test')).toBeTruthy();
    expect(screen.getByText(/Liée par un administrateur/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Lier mon identité/ })).toBeNull();
  });

  // req: EF-USR-004
  it('starts a link from the own session and goes to the provider', async () => {
    let started = 0;
    const assign = vi.fn();
    vi.stubGlobal('location', { ...window.location, assign, search: '' });
    server.use(
      http.get(`${BASE}/api/v1/auth/oidc/identities`, () => HttpResponse.json({ items: [] })),
      http.post(`${BASE}/api/v1/auth/oidc/link`, () => {
        started += 1;
        return HttpResponse.json({ authorization_url: 'https://idp.example.test/authorize?x=1' });
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /Lier mon identité/ }));
    await vi.waitFor(() => expect(started).toBe(1));
    expect(assign).toHaveBeenCalledWith('https://idp.example.test/authorize?x=1');
    vi.unstubAllGlobals();
  });
});
