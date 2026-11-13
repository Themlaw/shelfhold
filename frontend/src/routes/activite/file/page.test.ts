import { render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const REQUEST = '01JREQUEST00000000000000AB';
const server = setupServer();

const window_ = {
  timezone: 'Europe/Paris',
  start: '03:00',
  end: '07:00',
  crosses_midnight: false,
  current: null,
  next: {
    start_utc: '2026-11-04T02:00:00Z',
    end_utc: '2026-11-04T06:00:00Z',
    start_local: '03:00',
    end_local: '07:00',
    duration_s: 14400,
    open: false
  },
  upcoming: []
};
const state = (active: boolean, paused: string[] = [], parked = 0) =>
  HttpResponse.json({
    maintenance: { active, since: active ? '2026-11-03T09:00:00Z' : null, running_jobs: active ? 2 : 0 },
    queue: { paused, parked_jobs: parked },
    window: window_
  });
const job = (over: Record<string, unknown>) => ({
  id: 'J1',
  kind: 'export.run',
  status: 'queued',
  error_code: null,
  last_error: null,
  attempts: 0,
  request_id: null,
  priority_class: 'new',
  source_key: null,
  progress: null,
  ...over
});
const listing = (items: unknown[]) => HttpResponse.json({ items, next_cursor: null });
const problem = (status: number, extra: Record<string, unknown> = {}) =>
  HttpResponse.json({ type: '/api/v1/problems/x', title: 'x', status, code: 'x', ...extra }, { status });

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
  vi.stubGlobal(
    'EventSource',
    class {
      addEventListener() {}
      close() {}
    }
  );
});
afterEach(() => {
  server.resetHandlers();
  vi.unstubAllGlobals();
});
afterAll(() => server.close());

describe('Activité : file et maintenance', () => {
  // req: EF-INT-017
  it('test_queue_controls_screen', async () => {
    const posts: { url: string; body: unknown; csrf: string | null }[] = [];
    const record = (url: string, body: unknown, request: Request) =>
      posts.push({ url, body, csrf: request.headers.get('X-CSRF-Token') });
    server.use(
      http.get(`${BASE}/api/v1/maintenance`, () => state(false, ['source:alpha'], 3)),
      http.get(`${BASE}/api/v1/jobs`, () =>
        listing([
          job({ id: 'J1', request_id: REQUEST, source_key: 'alpha', kind: 'fetch.chapter' }),
          job({ id: 'J2', status: 'paused', error_code: 'window.preempted', kind: 'window.scan' }),
          job({ id: 'J3', status: 'dead_letter', kind: 'import.check' }),
          job({ id: 'J4', status: 'succeeded', kind: 'done.job' }),
          job({ id: 'J5', status: 'running', kind: 'run.job' })
        ])
      ),
      http.post(`${BASE}/api/v1/queue/pause`, async ({ request }) => {
        record('pause', await request.json(), request);
        return HttpResponse.json({ scope: 'all', parked: 1 });
      }),
      http.post(`${BASE}/api/v1/queue/resume`, async ({ request }) => {
        record('resume', await request.json(), request);
        return HttpResponse.json({ scope: 'alpha', resumed: 1 });
      }),
      http.post(`${BASE}/api/v1/jobs/J1/cancel`, ({ request }) => {
        record('cancel', null, request);
        return HttpResponse.json({ id: 'J1', outcome: 'cancelled' });
      }),
      http.post(`${BASE}/api/v1/jobs/J1/priority`, async ({ request }) => {
        record('priority', await request.json(), request);
        return HttpResponse.json({ id: 'J1', priority_class: 'backfill' });
      }),
      http.post(`${BASE}/api/v1/jobs/J3/replay`, ({ request }) => {
        record('replay', null, request);
        return HttpResponse.json({ id: 'J3', replayed: true });
      })
    );
    render(Page);
    const user = userEvent.setup();
    expect(await screen.findByText('fetch.chapter')).toBeInTheDocument();
    // « pour qui » comes from request_id; a preempted job is shown as such
    expect(screen.getByText(`Pour la demande ${REQUEST.slice(-8)}`)).toBeInTheDocument();
    expect(screen.getByText(/Préemptée à la fin de la fenêtre/)).toBeInTheDocument();
    // each job offers only what its state allows
    expect(screen.getByRole('button', { name: 'Annuler la tâche fetch.chapter' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Annuler la tâche run.job' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Annuler la tâche done.job' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Rejouer la tâche done.job' })).toBeNull();
    expect(screen.queryByRole('combobox', { name: /Priorité de run.job/ })).toBeNull();
    // global pause, source resume, cancel, priority, replay of an « À vérifier » job
    await user.click(screen.getByRole('button', { name: 'Mettre toute la file en pause' }));
    await user.click(screen.getByRole('button', { name: 'Reprendre alpha' }));
    await user.click(screen.getByRole('button', { name: 'Annuler la tâche fetch.chapter' }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Priorité de fetch.chapter' }), 'backfill');
    await user.click(screen.getByRole('button', { name: 'Rejouer la tâche import.check' }));
    await waitFor(() => expect(posts.map((p) => p.url)).toEqual(['pause', 'resume', 'cancel', 'priority', 'replay']));
    expect(posts[0]?.body).toEqual({ source: null });
    expect(posts[1]?.body).toEqual({ source: 'alpha' });
    expect(posts[3]?.body).toEqual({ priority_class: 'backfill' });
    for (const p of posts) expect(p.csrf).toBe('tok123');
  });

  // req: EF-INT-018
  it('test_maintenance_toggle_shows_frozen_state', async () => {
    let exits = 0;
    let other = 0;
    server.use(
      http.get(`${BASE}/api/v1/maintenance`, () => state(true, ['all'], 1)),
      http.get(`${BASE}/api/v1/jobs`, () => listing([job({ id: 'J1', kind: 'fetch.chapter' })])),
      http.post(`${BASE}/api/v1/maintenance/exit`, () => {
        exits += 1;
        return HttpResponse.json({ active: false, changed: true });
      }),
      http.post(`${BASE}/api/v1/queue/resume`, () => ((other += 1), HttpResponse.json({}))),
      http.post(`${BASE}/api/v1/jobs/J1/cancel`, () => ((other += 1), HttpResponse.json({})))
    );
    render(Page);
    const user = userEvent.setup();
    expect(await screen.findByText('Mode maintenance actif')).toBeInTheDocument();
    expect(screen.getByText('Gelé')).toBeInTheDocument();
    // the screen offers nothing but the exit: the writes are shown off and none reaches the server
    expect(screen.getByRole('button', { name: 'Reprendre toute la file' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Annuler la tâche fetch.chapter' })).toBeDisabled();
    expect(screen.getByRole('combobox', { name: 'Priorité de fetch.chapter' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Reprendre toute la file' }));
    await user.click(screen.getByRole('button', { name: 'Annuler la tâche fetch.chapter' }));
    expect(other).toBe(0);
    await user.click(screen.getByRole('button', { name: 'Sortir de maintenance' }));
    await waitFor(() => expect(exits).toBe(1));
  });

  // req: EF-INT-018
  it('test_maintenance_toggle_enter_when_inactive', async () => {
    let entered = 0;
    server.use(
      http.get(`${BASE}/api/v1/maintenance`, () => state(false)),
      http.get(`${BASE}/api/v1/jobs`, () => listing([])),
      http.post(`${BASE}/api/v1/maintenance/enter`, () => {
        entered += 1;
        return HttpResponse.json({ active: true, changed: true, running_jobs: 0 });
      })
    );
    render(Page);
    expect(await screen.findByText('Mode maintenance inactif')).toBeInTheDocument();
    expect(screen.getByText('Aucune tâche en file.')).toBeInTheDocument();
    const user = userEvent.setup();
    const button = screen.getByRole('button', { name: 'Entrer en maintenance' });
    button.focus(); // keyboard: the toggle is reachable and fires on Enter
    await user.keyboard('{Enter}');
    await waitFor(() => expect(entered).toBe(1));
  });

  // req: EF-INT-017
  it('test_queue_controls_refused_to_non_admin_shows_no_control', async () => {
    server.use(
      http.get(`${BASE}/api/v1/maintenance`, () => problem(403, { code: 'forbidden' })),
      http.get(`${BASE}/api/v1/jobs`, () => problem(403, { code: 'forbidden' }))
    );
    render(Page);
    expect(await screen.findByText(/réservé à l’administrateur/)).toBeInTheDocument();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });

  // req: EF-INT-018
  it('test_maintenance_toggle_asks_a_fresh_session_in_place_then_retries', async () => {
    let attempts = 0;
    server.use(
      http.get(`${BASE}/api/v1/maintenance`, () => state(false)),
      http.get(`${BASE}/api/v1/jobs`, () => listing([])),
      http.post(`${BASE}/api/v1/maintenance/enter`, () => {
        attempts += 1;
        return attempts === 1
          ? problem(401, { code: 'reauth_required', reauth_methods: ['password'] })
          : HttpResponse.json({ active: true, changed: true, running_jobs: 0 });
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, () => HttpResponse.json({ ok: true }))
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Entrer en maintenance' }));
    await user.type(await screen.findByLabelText(/mot de passe/i), 'pw');
    await user.click(screen.getByRole('button', { name: /Confirmer/ }));
    await waitFor(() => expect(attempts).toBe(2));
  });

  // req: EF-INT-017
  it('test_queue_action_failure_is_said_not_swallowed', async () => {
    server.use(
      http.get(`${BASE}/api/v1/maintenance`, () => state(false)),
      http.get(`${BASE}/api/v1/jobs`, () => listing([])),
      http.post(`${BASE}/api/v1/queue/pause`, () => problem(503, { code: 'maintenance.active' }))
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Mettre toute la file en pause' }));
    expect(await screen.findByText(/n’a pas pu être lue ou modifiée/)).toBeInTheDocument();
  });
});
