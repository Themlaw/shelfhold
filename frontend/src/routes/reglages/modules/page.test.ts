import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();
const REPO_URL = 'https://index.example.org/plugins';
const PLUGIN = 'fake-plugin';

const stale = () =>
  HttpResponse.json(
    {
      type: '/api/v1/problems/reauth-required', title: 'x', status: 401, code: 'reauth_required',
      auth_age_s: 2000, max_age_s: 900, reauth_methods: ['password']
    },
    { status: 401 }
  );
const forbidden = () =>
  HttpResponse.json({ type: '/api/v1/problems/forbidden', title: 'x', status: 403, code: 'forbidden' }, { status: 403 });

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Écran des modules', () => {
  // req: EF-SRC-017
  it('lists the indexes with their url, fingerprint, version and expiry', async () => {
    server.use(
      http.get(`${BASE}/api/v1/plugin-repos`, () =>
        HttpResponse.json({ items: [{ url: REPO_URL, key_id: 'key-1', last_version: 7, expires: '2026-12-01T00:00:00Z', added_at: '2026-11-01T00:00:00Z' }] })
      ),
      http.get(`${BASE}/api/v1/plugins`, () => HttpResponse.json({ items: [] }))
    );
    render(Page);
    expect(await screen.findByRole('heading', { name: 'Modules', level: 1 })).toBeTruthy();
    expect(await screen.findByText(REPO_URL)).toBeTruthy();
    expect(await screen.findByText(/key-1/)).toBeTruthy();
  });

  // req: EF-SRC-017
  it('adds an index from its url and signer key', async () => {
    server.use(
      http.get(`${BASE}/api/v1/plugin-repos`, () => HttpResponse.json({ items: [] })),
      http.get(`${BASE}/api/v1/plugins`, () => HttpResponse.json({ items: [] })),
      http.post(`${BASE}/api/v1/plugin-repos`, () =>
        HttpResponse.json({ url: REPO_URL, key_id: 'key-1', last_version: 0, expires: null, added_at: '2026-11-01T00:00:00Z' }, { status: 201 })
      )
    );
    render(Page);
    await screen.findByRole('heading', { name: 'Modules', level: 1 });
    const urlInput = screen.getByLabelText(/URL de l.index/);
    await userEvent.type(urlInput, REPO_URL);
    const keyInput = screen.getByLabelText('Clé publique du signataire');
    await userEvent.type(keyInput, 'PUBKEY');
    await userEvent.click(screen.getByRole('button', { name: /Ajouter l.index/ }));
    expect(await screen.findByText(REPO_URL)).toBeTruthy();
  });

  // req: EF-SRC-017
  it('shows a 403 as a refusal', async () => {
    server.use(
      http.get(`${BASE}/api/v1/plugin-repos`, () => forbidden()),
      http.get(`${BASE}/api/v1/plugins`, () => HttpResponse.json({ items: [] }))
    );
    render(Page);
    expect((await screen.findAllByRole('alert')).length).toBeGreaterThan(0);
  });

  // req: EF-SRC-017
  it('opens the re-authentication prompt on a reserved action and never simulates it client side', async () => {
    server.use(
      http.get(`${BASE}/api/v1/plugin-repos`, () => HttpResponse.json({ items: [] })),
      http.get(`${BASE}/api/v1/plugins`, () =>
        HttpResponse.json({ items: [{ name: PLUGIN, version: '1.0.0', mode: 'isolated', status: 'disabled' }] })
      ),
      http.get(`${BASE}/api/v1/plugins/${PLUGIN}/attestations`, () => HttpResponse.json({ items: [] })),
      http.post(`${BASE}/api/v1/plugins/${PLUGIN}/enabled`, () => stale())
    );
    render(Page);
    const enable = await screen.findByRole('button', { name: 'Activer' });
    await userEvent.click(enable);
    expect(await screen.findByRole('region', { name: 'Confirmez votre identité' })).toBeTruthy();
  });

  // req: EF-SRC-017
  it('attests a value for a module', async () => {
    let posted: Record<string, unknown> | null = null;
    server.use(
      http.get(`${BASE}/api/v1/plugin-repos`, () => HttpResponse.json({ items: [] })),
      http.get(`${BASE}/api/v1/plugins`, () =>
        HttpResponse.json({ items: [{ name: PLUGIN, version: '1.0.0', mode: 'isolated', status: 'enabled' }] })
      ),
      http.get(`${BASE}/api/v1/plugins/${PLUGIN}/attestations`, () =>
        HttpResponse.json({ items: posted ? [{ plugin_key: PLUGIN, field: 'content_trust', value: 'publisher', plugin_version: '1.0.0', attested_at: '2026-11-01T00:00:00Z' }] : [] })
      ),
      http.post(`${BASE}/api/v1/plugins/${PLUGIN}/attestations`, async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ plugin_key: PLUGIN, field: 'content_trust', value: 'publisher', plugin_version: '1.0.0', attested_at: '2026-11-01T00:00:00Z' });
      })
    );
    render(Page);
    await screen.findByRole('heading', { name: 'Modules', level: 1 });
    await userEvent.type(screen.getByLabelText('Module'), PLUGIN);
    await userEvent.click(screen.getByRole('button', { name: 'Attester' }));
    expect(posted).toMatchObject({ field: 'content_trust', value: 'publisher' });
    expect(await screen.findByText(/content_trust: publisher/)).toBeTruthy();
  });

  // req: EF-UX-011
  it('shows the trust card with three lines', async () => {
    server.use(
      http.get(`${BASE}/api/v1/plugin-repos`, () => HttpResponse.json({ items: [] })),
      http.get(`${BASE}/api/v1/plugins`, () =>
        HttpResponse.json({ items: [{ name: PLUGIN, version: '1.0.0', mode: 'isolated', status: 'enabled' }] })
      ),
      http.get(`${BASE}/api/v1/plugins/${PLUGIN}/attestations`, () =>
        HttpResponse.json({ items: [{ plugin_key: PLUGIN, field: 'content_trust', value: 'publisher', plugin_version: '1.0.0', attested_at: '2026-11-01T00:00:00Z' }] })
      )
    );
    render(Page);
    const list = await screen.findByRole('list', { name: 'Niveau de confiance' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(3);
  });

  // req: EF-SRC-020
  it('updates, pins and rolls back a module', async () => {
    let pinned = false;
    let unpinned = false;
    server.use(
      http.get(`${BASE}/api/v1/plugin-repos`, () => HttpResponse.json({ items: [] })),
      http.get(`${BASE}/api/v1/plugins`, () =>
        HttpResponse.json({ items: [{ name: PLUGIN, version: '1.0.0', mode: 'isolated', status: 'enabled' }] })
      ),
      http.get(`${BASE}/api/v1/plugins/${PLUGIN}/attestations`, () => HttpResponse.json({ items: [] })),
      http.post(`${BASE}/api/v1/plugins/${PLUGIN}/update`, () =>
        HttpResponse.json({ name: PLUGIN, version: '1.1.0', mode: 'isolated', status: 'enabled' })
      ),
      http.post(`${BASE}/api/v1/plugins/${PLUGIN}/pin`, () => {
        pinned = true;
        return HttpResponse.json({ name: PLUGIN, version: '1.1.0', mode: 'isolated', status: 'enabled' });
      }),
      http.delete(`${BASE}/api/v1/plugins/${PLUGIN}/pin`, () => {
        unpinned = true;
        return HttpResponse.json({ name: PLUGIN, version: '1.1.0', mode: 'isolated', status: 'enabled' });
      }),
      http.post(`${BASE}/api/v1/plugins/${PLUGIN}/rollback`, () => stale())
    );
    render(Page);
    await userEvent.click(await screen.findByRole('button', { name: 'Mettre à jour' }));
    expect(await screen.findByText('1.1.0')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Épingler' }));
    expect(pinned).toBe(true);
    await userEvent.click(screen.getByRole('button', { name: 'Désépingler' }));
    expect(unpinned).toBe(true);
    await userEvent.click(screen.getByRole('button', { name: 'Retour arrière' }));
    expect(await screen.findByRole('region', { name: 'Confirmez votre identité' })).toBeTruthy();
  });
});
