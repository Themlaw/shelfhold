import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();

const KEY = 'fake-tracker';
const TOKEN = 'secret-token-value';

const stale = () =>
  HttpResponse.json(
    {
      type: '/api/v1/problems/reauth-required', title: 'x', status: 401, code: 'reauth_required',
      auth_age_s: 2000, max_age_s: 900, reauth_methods: ['password']
    },
    { status: 401 }
  );
const childRefused = () =>
  HttpResponse.json({ type: '/api/v1/problems/tracker_child_refused', title: 'x', status: 403, code: 'tracker_child_refused' }, { status: 403 });

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Écran des trackers', () => {
  it('links a tracker for the profile', async () => {
    server.use(
      http.get(`${BASE}/api/v1/me`, () => HttpResponse.json({ id: 'u1', role: 'member', username: 'm', nav: [] })),
      http.get(`${BASE}/api/v1/profiles/u1/trackers`, () => HttpResponse.json({ items: [] })),
      http.put(`${BASE}/api/v1/profiles/u1/trackers/${KEY}`, () =>
        HttpResponse.json({ plugin_key: KEY, linked_at: '2026-11-01T00:00:00Z', push_enabled: false, token_fingerprint: 'fp' })
      )
    );
    render(Page);
    await screen.findByRole('heading', { name: 'Trackers', level: 1 });
    await userEvent.type(screen.getByLabelText('Module'), KEY);
    await userEvent.type(screen.getByLabelText('Jeton'), TOKEN);
    await userEvent.click(screen.getByRole('button', { name: 'Lier' }));
    expect(await screen.findByText(KEY)).toBeTruthy();
  });

  it('unlinks a tracker', async () => {
    server.use(
      http.get(`${BASE}/api/v1/me`, () => HttpResponse.json({ id: 'u1', role: 'member', username: 'm', nav: [] })),
      http.get(`${BASE}/api/v1/profiles/u1/trackers`, () =>
        HttpResponse.json({ items: [{ plugin_key: KEY, linked_at: '2026-11-01T00:00:00Z', push_enabled: false }] })
      ),
      http.delete(`${BASE}/api/v1/profiles/u1/trackers/${KEY}`, () => new HttpResponse(null, { status: 204 }))
    );
    render(Page);
    await userEvent.click(await screen.findByRole('button', { name: 'Délier' }));
    expect(await screen.findByText('Aucun tracker lié.')).toBeTruthy();
  });

  it('toggles the push opt-in with a re-authentication prompt when required', async () => {
    server.use(
      http.get(`${BASE}/api/v1/me`, () => HttpResponse.json({ id: 'u1', role: 'member', username: 'm', nav: [] })),
      http.get(`${BASE}/api/v1/profiles/u1/trackers`, () =>
        HttpResponse.json({ items: [{ plugin_key: KEY, linked_at: '2026-11-01T00:00:00Z', push_enabled: false }] })
      ),
      http.put(`${BASE}/api/v1/profiles/u1/trackers/${KEY}/push`, () => stale())
    );
    render(Page);
    const box = await screen.findByRole('checkbox');
    await userEvent.click(box);
    expect(await screen.findByRole('region', { name: 'Confirmez votre identité' })).toBeTruthy();
  });

  it('a child without authorization does not see the screen content', async () => {
    server.use(
      http.get(`${BASE}/api/v1/me`, () => HttpResponse.json({ id: 'k1', role: 'child', username: 'kid', nav: [] })),
      http.get(`${BASE}/api/v1/profiles/k1/trackers`, () => childRefused())
    );
    render(Page);
    expect(await screen.findByText("Ce tracker n'est pas permis à un profil enfant.")).toBeTruthy();
    expect(screen.queryByRole('form')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Lier' })).toBeNull();
  });

  it('an authorized child sees the screen', async () => {
    server.use(
      http.get(`${BASE}/api/v1/me`, () => HttpResponse.json({ id: 'k1', role: 'child', username: 'kid', nav: [] })),
      http.get(`${BASE}/api/v1/profiles/k1/trackers`, () => HttpResponse.json({ items: [] }))
    );
    render(Page);
    expect(await screen.findByRole('heading', { name: 'Trackers', level: 1 })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Lier' })).toBeTruthy();
  });
});
