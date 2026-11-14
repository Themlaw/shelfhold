import { render, screen } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();

const source = (plugin_id: string, state: string, code: string | null, command?: string) => ({
  plugin_id,
  state,
  code,
  since: null,
  action: command ? { command, reserved: false, text: 'Que faire' } : null
});
const empty = {
  generated_at: '2026-11-13T09:00:00Z',
  irreplaceable: { count: 0, without_kept_copy: 0, single_copy: 0, store_backup: {} },
  lost_originals: [],
  withdrawal_pending: { count: 0, action: null },
  kavita_audit: [],
  library_type_changes: [],
  jurisdiction: null,
  backup: { last_ok_at: null, age_hours: null, warnings: [] },
  stats: { human_interventions: {}, units_exported: 0, arrivals_pending: 0 }
};
const serve = (sources: unknown[]) =>
  http.get(`${BASE}/api/v1/health/page`, () => HttpResponse.json({ ...empty, sources } as never));

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Sources page', () => {
  // req: EF-SRC-014
  it('lists the degraded and paused sources with the command that leaves it, and hides the others', async () => {
    server.use(
      serve([
        source('good', 'ok', null),
        source('down', 'degraded', 'source.broken', 'shelfhold source recheck'),
        source('expired', 'paused', 'source.auth_expired', 'shelfhold source set-secret expired')
      ])
    );
    render(Page);
    expect(await screen.findByText('source.broken')).toBeInTheDocument();
    expect(screen.getByText('shelfhold source recheck')).toBeInTheDocument();
    expect(screen.getByText('source.auth_expired')).toBeInTheDocument();
    expect(screen.getByText(/^down —/)).toBeInTheDocument();
    expect(screen.queryByText(/^good/)).toBeNull();
  });

  // req: EF-UX-008
  it('says so, and points to Santé, when every source is well', async () => {
    server.use(serve([source('good', 'ok', null)]));
    render(Page);
    expect(await screen.findByText('Toutes les sources répondent')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Ouvrir la page Santé' })).toHaveAttribute('href', '/sante');
  });

  // req: EF-SRC-014
  it('shows the refusal to a role the server refuses, never an empty list', async () => {
    server.use(
      http.get(`${BASE}/api/v1/health/page`, () =>
        HttpResponse.json(
          { type: '/api/v1/problems/forbidden', title: 'Accès refusé', status: 403, code: 'forbidden' },
          { status: 403 }
        )
      )
    );
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent('Accès refusé');
    expect(screen.queryByText('Toutes les sources répondent')).toBeNull();
  });
});
