import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import BulkBar from './BulkBar.svelte';

const BASE = 'http://shelfhold.test';
const WORK_A = '01JWORK0000000000000000001';
const WORK_B = '01JWORK0000000000000000002';
const ORDER = '01JORDER000000000000000001';
const TOKEN = 'tok-123';
const JOB = '01JJOB00000000000000000001';
const server = setupServer();

const items = [
  { work_id: WORK_A, title: 'Titre A', state: 'rating_pending', reason: 'r' },
  { work_id: WORK_B, title: 'Titre B', state: 'follow_suspended', reason: 'r' }
];

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  setUiLanguage('fr');
  document.cookie = 'csrf=tok123';
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function previewHandler(body: Record<string, unknown>) {
  return HttpResponse.json({
    order_id: ORDER,
    age: body.age ?? '13+',
    token: TOKEN,
    skipped: 0,
    expires_at: '2026-11-17T12:00:00Z',
    items: [{ work_id: WORK_A, title: 'Titre A', state: 'rating_pending' }]
  });
}

describe('BulkBar', () => {
  // req: EF-BIB-023
  it('test_bulk_bar_applies_only_to_ticked_works', async () => {
    let previewBody: Record<string, unknown> | null = null;
    let applyCalled = false;
    server.use(
      http.post(`${BASE}/api/v1/bulk/rate/preview`, async ({ request }) => {
        previewBody = (await request.json()) as Record<string, unknown>;
        return previewHandler(previewBody);
      }),
      http.post(`${BASE}/api/v1/bulk/rate/${ORDER}/apply`, async () => {
        applyCalled = true;
        return HttpResponse.json({ job_id: JOB, status_url: `/api/v1/jobs/${JOB}` }, { status: 202 });
      }),
      http.get(`${BASE}/api/v1/jobs/${JOB}`, () =>
        HttpResponse.json({ id: JOB, status: 'succeeded', error_code: null, kind: 'bulk_rate', attempts: 1, created_at: null, finished_at: null, progress: null, started_at: null })
      )
    );
    const onchanged = vi.fn();
    render(BulkBar, { items: items as never, stateFilter: '', textFilter: '', onchanged } as never);
    const user = userEvent.setup();
    const tick = screen.getByRole('checkbox', { name: 'Sélectionner Titre A' });
    await user.click(tick);
    // AgePicker legend may differ; pick radio by name
    await user.click(screen.getByRole('radio', { name: '13+' }));
    await user.click(screen.getByRole('button', { name: 'Aperçu' }));
    expect(await screen.findByText(/classée/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Appliquer' }));
    await screen.findByText(/Œuvre.*classée/);
    expect(previewBody).toMatchObject({ work_ids: [WORK_A], age: '13+' });
    expect(previewBody).not.toHaveProperty('filter');
    expect(applyCalled).toBe(true);
    expect(onchanged).toHaveBeenCalled();
  });

  // req: EF-BIB-023
  it('test_bulk_bar_preview_before_apply_and_cancel_calls_nothing', async () => {
    let applyCalled = false;
    server.use(
      http.post(`${BASE}/api/v1/bulk/rate/preview`, async ({ request }) => {
        const body = await request.json();
        return previewHandler(body as Record<string, unknown>);
      }),
      http.post(`${BASE}/api/v1/bulk/rate/${ORDER}/apply`, async () => {
        applyCalled = true;
        return HttpResponse.json({ job_id: JOB, status_url: `/api/v1/jobs/${JOB}` }, { status: 202 });
      })
    );
    render(BulkBar, { items: items as never, stateFilter: '', textFilter: '' } as never);
    const user = userEvent.setup();
    await user.click(screen.getByRole('checkbox', { name: 'Sélectionner Titre A' }));
    await user.click(screen.getByRole('radio', { name: '13+' }));
    await user.click(screen.getByRole('button', { name: 'Aperçu' }));
    await screen.findByText(/classée/);
    await user.click(screen.getByRole('button', { name: 'Annuler' }));
    expect(screen.queryByText(/classée/)).toBeNull();
    expect(applyCalled).toBe(false);
  });

  // req: EF-BIB-023
  it('test_bulk_bar_filter_mode_sends_filter_not_ids', async () => {
    let previewBody: Record<string, unknown> | null = null;
    server.use(
      http.post(`${BASE}/api/v1/bulk/rate/preview`, async ({ request }) => {
        previewBody = (await request.json()) as Record<string, unknown>;
        return previewHandler(previewBody);
      })
    );
    render(BulkBar, { items: items as never, stateFilter: 'rating_pending', textFilter: 'Tit', onchanged: vi.fn() } as never);
    const user = userEvent.setup();
    await user.click(screen.getByRole('checkbox', { name: /Toutes celles qui correspondent/ }));
    await user.click(screen.getByRole('radio', { name: '13+' }));
    await user.click(screen.getByRole('button', { name: 'Aperçu' }));
    await screen.findByText(/classée/);
    expect(previewBody).not.toHaveProperty('work_ids');
    expect(previewBody).toHaveProperty('filter');
  });

  // req: EF-BIB-023
  it('test_bulk_bar_titles_are_text_not_markup', async () => {
    const xssItems = [{ work_id: WORK_A, title: '<img src=x onerror=alert(1)> Titre', state: 'rating_pending', reason: 'r' }];
    const { container } = render(BulkBar, { items: xssItems as never, stateFilter: '', textFilter: '' } as never);
    expect(screen.getByText('<img src=x onerror=alert(1)> Titre')).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
  });

  // req: EF-BIB-023
  it('test_bulk_bar_stale_job_says_nothing_was_rated', async () => {
    server.use(
      http.post(`${BASE}/api/v1/bulk/rate/preview`, async ({ request }) => {
        const body = await request.json();
        return previewHandler(body as Record<string, unknown>);
      }),
      http.post(`${BASE}/api/v1/bulk/rate/${ORDER}/apply`, async () =>
        HttpResponse.json({ job_id: JOB, status_url: `/api/v1/jobs/${JOB}` }, { status: 202 })
      ),
      http.get(`${BASE}/api/v1/jobs/${JOB}`, () =>
        HttpResponse.json({ id: JOB, status: 'failed', error_code: 'bulk.stale', kind: 'bulk_rate', attempts: 1, created_at: null, finished_at: null, progress: null, started_at: null })
      )
    );
    render(BulkBar, { items: items as never, stateFilter: '', textFilter: '' } as never);
    const user = userEvent.setup();
    await user.click(screen.getByRole('checkbox', { name: 'Sélectionner Titre A' }));
    await user.click(screen.getByRole('radio', { name: '13+' }));
    await user.click(screen.getByRole('button', { name: 'Aperçu' }));
    await screen.findByText(/classée/);
    await user.click(screen.getByRole('button', { name: 'Appliquer' }));
    expect(await screen.findByText(/Rien n'a été classé/)).toBeInTheDocument();
  });

  // req: EF-BIB-023
  it('test_bulk_bar_needs_fresh_session_asks_in_place', async () => {
    let calls = 0;
    server.use(
      http.post(`${BASE}/api/v1/bulk/rate/preview`, async ({ request }) => {
        calls += 1;
        if (calls === 1) {
          return HttpResponse.json(
            { type: '/api/v1/problems/x', title: 'x', status: 401, code: 'reauth_required', reauth_methods: ['password'] },
            { status: 401 }
          );
        }
        const body = await request.json();
        return previewHandler(body as Record<string, unknown>);
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, () =>
        HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' })
      )
    );
    render(BulkBar, { items: items as never, stateFilter: '', textFilter: '' } as never);
    const user = userEvent.setup();
    await user.click(screen.getByRole('checkbox', { name: 'Sélectionner Titre A' }));
    await user.click(screen.getByRole('radio', { name: '13+' }));
    await user.click(screen.getByRole('button', { name: 'Aperçu' }));
    await user.type(await screen.findByLabelText('Mot de passe'), 'secret');
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    expect(await screen.findByText(/classée/)).toBeInTheDocument();
    expect(calls).toBe(2);
  });
});
