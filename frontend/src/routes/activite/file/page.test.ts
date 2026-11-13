import { render, screen, waitFor } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer(
  http.get(`${BASE}/api/v1/maintenance`, () =>
    HttpResponse.json({
      maintenance: { active: false, since: null, running_jobs: 0 },
      queue: { paused: [], parked_jobs: 0 },
      window: {
        timezone: 'Europe/Paris',
        start: '03:00',
        end: '07:00',
        crosses_midnight: false,
        current: null,
        next: { start_utc: '2026-11-04T02:00:00Z', end_utc: '2026-11-04T06:00:00Z', start_local: 'x', end_local: 'y', duration_s: 14400, open: false },
        upcoming: []
      }
    })
  ),
  http.get(`${BASE}/api/v1/jobs`, () =>
    HttpResponse.json({
      items: [
        {
          id: 'job1',
          kind: 'export.run',
          status: 'queued',
          error_code: null,
          last_error: null,
          attempts: 0,
          request_id: 'req42',
          priority_class: 'backfill',
          source_key: 'src1'
        }
      ],
      next_cursor: null
    })
  )
);
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  vi.stubGlobal('EventSource', class {
    addEventListener() {}
    close() {}
  });
});
afterEach(() => {
  server.resetHandlers();
  vi.unstubAllGlobals();
});
afterAll(() => server.close());

describe('activite file page', () => {
  it('test_queue_controls_screen', async () => {
    render(Page);
    expect(await screen.findByText('File d'attente')).toBeInTheDocument();
    expect(await screen.findByText('export.run')).toBeInTheDocument();
    expect(screen.getByText('Pour : req42')).toBeInTheDocument();
  });

  it('test_maintenance_toggle_shows_frozen_state', async () => {
    server.use(
      http.get(`${BASE}/api/v1/maintenance`, () =>
        HttpResponse.json({
          maintenance: { active: true, since: '2026-11-03T09:00:00Z', running_jobs: 2 },
          queue: { paused: ['all'], parked_jobs: 1 },
          window: {
            timezone: 'Europe/Paris',
            start: '03:00',
            end: '07:00',
            crosses_midnight: false,
            current: null,
            next: { start_utc: '2026-11-04T02:00:00Z', end_utc: '2026-11-04T06:00:00Z', start_local: 'x', end_local: 'y', duration_s: 14400, open: false },
            upcoming: []
          }
        })
      )
    );
    render(Page);
    expect(await screen.findByText('Mode maintenance actif')).toBeInTheDocument();
    expect(screen.getByText('Gelé')).toBeInTheDocument();
  });
});