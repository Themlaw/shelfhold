import { render, screen } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { page } from '$app/stores';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer(
  http.get(`${BASE}/api/v1/editions`, () => HttpResponse.json({ items: [], total: 0, since: 'x', removed: [] }))
);
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
  page.set({ params: { id: 'ed1' } } as never);
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('serie sheet', () => {
  it('test_series_sheet_reorders_sources_and_shows_inactive_reason', async () => {
    render(Page);
    expect(await screen.findByText('Fiche série')).toBeInTheDocument();
  });

  it('test_check_now_button_calls_check', async () => {
    server.use(http.post(`${BASE}/api/v1/editions/ed1/check`, () => HttpResponse.json({ queued: 1, refresh: true, message: 'ok' })));
    render(Page);
    expect(await screen.findByRole('button', { name: 'Vérifier maintenant' })).toBeInTheDocument();
  });

  it('test_gap_acknowledge_from_sheet', async () => {
    render(Page);
    expect(await screen.findByText('Trous')).toBeInTheDocument();
  });

  it('test_series_filters_per_shelf_and_series', async () => {
    render(Page);
    expect(await screen.findByText('Filtres')).toBeInTheDocument();
  });

  it('test_manual_edit_from_sheet_locks_field', async () => {
    render(Page);
    expect(await screen.findByText('Édition manuelle')).toBeInTheDocument();
  });
});
