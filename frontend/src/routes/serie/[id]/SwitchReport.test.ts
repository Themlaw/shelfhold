import { render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import SwitchReport from './SwitchReport.svelte';

const BASE = 'http://shelfhold.test';
const ROOT = `${BASE}/api/v1/editions/E1/team-switch`;
const server = setupServer();

const view = (extra: Record<string, unknown> = {}) => ({
  id: 'S1',
  edition_id: 'E1',
  team_id: 'T2',
  state: 'previewed',
  to_switch: ['U1', 'U2'],
  blocked: { U3: ['needs_confirmation'], U4: ['adopted_original'] },
  pending: ['U5'],
  switched: [],
  created_at: '2026-11-17T12:00:00Z',
  ...extra
});

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
  setUiLanguage('fr');
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Switch to the preferred team', () => {
  // req: EF-BIB-005
  it('test_switch_report_lists_switched_blocked_pending', async () => {
    let applied = false;
    server.use(
      http.post(ROOT, ({ request }) => {
        expect(request.headers.get('x-csrf-token')).toBe('tok123');
        return HttpResponse.json(view(), { status: 201 });
      }),
      http.post(`${ROOT}/S1/apply`, () => {
        applied = true;
        return HttpResponse.json({ job_id: 'J1', status_url: '/api/v1/jobs/J1' }, { status: 202 });
      }),
      http.get(`${ROOT}/S1`, () =>
        HttpResponse.json(
          view({ state: 'done', to_switch: [], switched: ['U1', 'U2'] })
        )
      )
    );
    render(SwitchReport, { editionId: 'E1' });
    await userEvent.click(screen.getByRole('button', { name: /Voir ce qui serait remplacé/ }));
    // the preview says what would be replaced, what is refused and what waits: nothing applied yet
    expect(await screen.findByText(/2 chapitres seront remplacés/)).toBeInTheDocument();
    expect(screen.getByText(/2 chapitres sont bloqués/)).toBeInTheDocument();
    expect(screen.getByText(/1 chapitre attend/)).toBeInTheDocument();
    expect(applied).toBe(false);
    // the reasons are words of the screen, never the raw codes
    expect(screen.getByText(/Original irremplaçable/)).toBeInTheDocument();
    expect(screen.getByText(/Original adopté/)).toBeInTheDocument();
    expect(screen.queryByText('needs_confirmation')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /Remplacer maintenant/ }));
    // the result: switched, blocked, pending
    expect(await screen.findByRole('heading', { name: /Résultat/ })).toBeInTheDocument();
    expect(screen.getByText(/2 chapitres remplacés/)).toBeInTheDocument();
    expect(screen.getByText(/2 chapitres bloqués/)).toBeInTheDocument();
    expect(screen.getByText(/1 chapitre en attente/)).toBeInTheDocument();
    expect(applied).toBe(true);
  });

  // req: EF-BIB-005
  it('test_nothing_to_switch_draws_no_apply_button', async () => {
    server.use(
      http.post(ROOT, () =>
        HttpResponse.json(view({ to_switch: [], blocked: {}, pending: [] }), { status: 201 })
      )
    );
    render(SwitchReport, { editionId: 'E1' });
    await userEvent.click(screen.getByRole('button', { name: /Voir ce qui serait remplacé/ }));
    expect(await screen.findByText(/Rien à remplacer/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Remplacer maintenant/ })).toBeNull();
  });

  // req: EF-BIB-005
  it('test_a_refused_session_shows_the_problem_and_applies_nothing', async () => {
    let applied = false;
    server.use(
      http.post(ROOT, () =>
        HttpResponse.json(
          { type: '/api/v1/problems/reauth-required', title: 'reauth', status: 401 },
          { status: 401 }
        )
      ),
      http.post(`${ROOT}/S1/apply`, () => {
        applied = true;
        return HttpResponse.json({}, { status: 202 });
      })
    );
    render(SwitchReport, { editionId: 'E1' });
    await userEvent.click(screen.getByRole('button', { name: /Voir ce qui serait remplacé/ }));
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /Remplacer maintenant/ })).toBeNull();
    expect(applied).toBe(false);
  });

  // req: EF-BIB-005
  it('test_a_stale_preview_asks_for_a_new_one', async () => {
    server.use(
      http.post(ROOT, () => HttpResponse.json(view(), { status: 201 })),
      http.post(`${ROOT}/S1/apply`, () =>
        HttpResponse.json({ job_id: 'J1', status_url: '/api/v1/jobs/J1' }, { status: 202 })
      ),
      http.get(`${ROOT}/S1`, () => HttpResponse.json(view({ state: 'stale' })))
    );
    render(SwitchReport, { editionId: 'E1' });
    await userEvent.click(screen.getByRole('button', { name: /Voir ce qui serait remplacé/ }));
    await userEvent.click(await screen.findByRole('button', { name: /Remplacer maintenant/ }));
    expect(await screen.findByText(/L'aperçu n'est plus à jour/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Voir ce qui serait remplacé/ })).toBeInTheDocument();
  });
});
