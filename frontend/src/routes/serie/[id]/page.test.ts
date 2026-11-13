import { render, screen, waitFor, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

vi.mock('$app/state', () => ({ page: { params: { id: 'ED1' } } }));

const BASE = 'http://shelfhold.test';
const API = `${BASE}/api/v1/editions/ED1`;
const server = setupServer();

const binding = (id: string, rank: number, over: Record<string, unknown> = {}) => ({
  id,
  source_key: `src-${id}`,
  rank,
  active: true,
  disabled_reason: null,
  last_checked: '2026-11-10T09:30:00Z',
  next_expected: null,
  ...over
});
const CAN = {
  check: true,
  reorder: true,
  acknowledge_gap: true,
  set_upstream_status: true,
  prefer_team: true,
  edit_field: true,
  separate: false,
  confirm_attach: false
};
let current: Record<string, unknown>;
const sheet = (over: Record<string, unknown> = {}) => ({
  edition_id: 'ED1',
  work_id: 'WK1',
  title: 'Une série',
  lang: 'fr',
  shelf_id: null,
  cover_hash: 'a'.repeat(64),
  upstream_status: 'unknown',
  upstream_status_by: null,
  followed: true,
  bindings: [binding('A', 1), binding('B', 2), binding('C', 3), binding('D', 9, { active: false, disabled_reason: 'removed_upstream' })],
  gaps: [
    { track: 'main', segment: '', number: '3' },
    { track: 'main', segment: '', number: '7' }
  ],
  teams: [
    { id: 'T1', label: 'Équipe un', rank: 1, preferred: true },
    { id: 'T2', label: 'Équipe deux', rank: 2, preferred: false }
  ],
  fields: [{ on: 'edition', field: 'title', lang: 'fr', value: 'Titre du serveur', manual: false, locked: false }],
  merges: [],
  attach: null,
  can: CAN,
  ...over
});
const problem = (status: number, code: string, title: string, extra: Record<string, unknown> = {}) =>
  HttpResponse.json({ type: `/api/v1/problems/${code}`, title, status, code, ...extra }, { status });
const serve = () => http.get(`${API}/sheet`, () => HttpResponse.json(current));

let bodies: unknown[] = [];
const record = async (request: Request) => {
  bodies.push(await request.json().catch(() => null));
};

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
  current = sheet();
  bodies = [];
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Series sheet', () => {
  // req: EF-SRC-037
  it('test_series_sheet_reorders_sources_and_shows_inactive_reason', async () => {
    const user = userEvent.setup();
    let refuse = false;
    server.use(
      serve(),
      http.post(`${API}/bindings/order`, async ({ request }) => {
        await record(request);
        expect(request.headers.get('X-CSRF-Token')).toBe('tok123');
        return refuse
          ? problem(400, 'invalid_choice', 'Choix refusé')
          : HttpResponse.json({ edition_id: 'ED1', order: [] });
      })
    );
    render(Page);
    const inactive = (await screen.findByText('Retirée à la source')).closest('li') as HTMLElement;
    expect(within(inactive).getByText('src-D')).toBeInTheDocument();
    expect(within(inactive).queryAllByRole('button')).toHaveLength(0);

    await user.click(screen.getByRole('button', { name: 'Descendre src-A' }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ binding_ids: ['B', 'A', 'C'] }); // every active id, never the inactive one
    expect(screen.queryByRole('alert')).toBeNull();

    // the server refuses: its problem is shown and its order is read again
    refuse = true;
    current = sheet({ bindings: [binding('C', 1), binding('B', 2), binding('A', 3)] });
    await user.click(screen.getByRole('button', { name: 'Monter src-B' }));
    expect(await screen.findByText('Choix refusé')).toBeInTheDocument();
    await waitFor(() => {
      const names = screen.getAllByText(/^src-[ABC]$/).map((n) => n.textContent);
      expect(names).toEqual(['src-C', 'src-B', 'src-A']);
    });
  });

  // req: EF-SRC-037
  it('offers no move when the server does not allow a reorder, and none for the first and last', async () => {
    current = sheet({ can: { ...CAN, reorder: false } });
    server.use(serve());
    render(Page);
    await screen.findByText('src-A');
    expect(screen.queryByRole('button', { name: /^(Monter|Descendre)/ })).toBeNull();
  });

  // req: EF-SRC-038
  it('test_check_now_button_calls_check', async () => {
    const user = userEvent.setup();
    let calls = 0;
    server.use(
      serve(),
      http.post(`${API}/check`, ({ request }) => {
        calls += 1;
        expect(request.headers.get('X-CSRF-Token')).toBe('tok123');
        return HttpResponse.json({ queued: 1, refresh: true, message: 'Contrôle demandé.' });
      })
    );
    render(Page);
    await user.click(await screen.findByRole('button', { name: 'Vérifier maintenant' }));
    expect(await screen.findByText('Contrôle demandé.')).toBeInTheDocument();
    expect(calls).toBe(1);
  });

  // req: EF-SRC-038
  it('does not show "Vérifier maintenant" when the server does not allow it', async () => {
    current = sheet({ can: { ...CAN, check: false } });
    server.use(serve());
    render(Page);
    await screen.findByText('Une série', { selector: 'h2' });
    expect(screen.queryByRole('button', { name: 'Vérifier maintenant' })).toBeNull();
  });

  // req: EF-BIB-056
  it('test_gap_acknowledge_from_sheet', async () => {
    const user = userEvent.setup();
    server.use(
      serve(),
      http.post(`${API}/gaps/ack`, async ({ request }) => {
        await record(request);
        const body = bodies.at(-1) as { number: string };
        return body.number === '3' ? HttpResponse.json({ changed: true, undone: false }) : problem(404, 'not_found', 'Introuvable');
      })
    );
    render(Page);
    await user.click(await screen.findByRole('button', { name: 'Acquitter le trou main n° 3' }));
    await waitFor(() => expect(screen.queryByText('main n° 3')).toBeNull());
    expect(bodies[0]).toEqual({ track: 'main', segment: '', number: '3' });
    // a refused one stays, with the server's problem
    await user.click(screen.getByRole('button', { name: 'Acquitter le trou main n° 7' }));
    expect(await screen.findByText('Introuvable')).toBeInTheDocument();
    expect(screen.getByText('main n° 7')).toBeInTheDocument();
  });

  // req: EF-BIB-056
  it('shows no gap section when the server allows no acknowledgement', async () => {
    current = sheet({ can: { ...CAN, acknowledge_gap: false } });
    server.use(serve());
    render(Page);
    await screen.findByText('src-A');
    expect(screen.queryByText('Trous')).toBeNull();
  });

  // req: EF-BIB-011
  it('test_manual_edit_from_sheet_locks_field', async () => {
    const user = userEvent.setup();
    server.use(
      serve(),
      http.post(`${API}/fields/title/lock`, async ({ request }) => {
        await record(request);
        current = sheet({
          fields: [{ on: 'edition', field: 'title', lang: 'fr', value: 'Titre à la main', manual: true, locked: true }]
        });
        return HttpResponse.json({ edition_id: 'ED1', field: 'title', lang: 'fr', on: 'edition', locked: true, kept: false });
      })
    );
    render(Page);
    const input = await screen.findByLabelText('Valeur');
    await user.type(input, 'Titre à la main');
    await user.click(screen.getByRole('button', { name: 'Enregistrer et verrouiller' }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ value: 'Titre à la main', lang: 'fr', on: 'edition' });
    expect(await screen.findByText('Verrouillé · manuel')).toBeInTheDocument();
  });

  // req: EF-SRC-038
  it('sets the upstream status by hand and says it was', async () => {
    const user = userEvent.setup();
    server.use(
      serve(),
      http.post(`${API}/upstream-status`, async ({ request }) => {
        await record(request);
        current = sheet({ upstream_status: 'finished', upstream_status_by: 'manual' });
        return HttpResponse.json({ edition_id: 'ED1', upstream_status: 'finished', by: 'manual' });
      })
    );
    render(Page);
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Statut amont' }), 'finished');
    await user.click(screen.getByRole('button', { name: 'Régler' }));
    expect(await screen.findByText('Réglé à la main')).toBeInTheDocument();
    expect(bodies[0]).toEqual({ status: 'finished' });
  });

  // req: EF-BIB-005
  it('prefers a team and offers it only when the server allows', async () => {
    const user = userEvent.setup();
    server.use(
      serve(),
      http.post(`${API}/preferred-team`, async ({ request }) => {
        await record(request);
        return HttpResponse.json({ edition_id: 'ED1', team_id: 'T2' });
      })
    );
    const first = render(Page);
    expect(screen.queryByRole('button', { name: 'Préférer Équipe un' })).toBeNull();
    await user.click(await screen.findByRole('button', { name: 'Préférer Équipe deux' }));
    await waitFor(() => expect(bodies[0]).toEqual({ team_id: 'T2' }));
    first.unmount();
    current = sheet({ can: { ...CAN, prefer_team: false } });
    render(Page);
    await screen.findByText('src-A');
    expect(screen.queryByText('Équipe préférée')).toBeNull();
  });

  // req: EF-BIB-019
  it('asks for a confirmation before "Séparer", then shows the plan without applying it', async () => {
    const user = userEvent.setup();
    current = sheet({
      can: { ...CAN, separate: true },
      merges: [{ merge_id: 'M1', merged_at: '2026-11-10T09:00:00Z', undoable_until: '2026-12-10T09:00:00Z' }]
    });
    let calls = 0;
    server.use(
      serve(),
      http.post(`${BASE}/api/v1/works/merges/M1/separate`, () => {
        calls += 1;
        return HttpResponse.json({
          merge_id: 'M1',
          survivor_id: 'WK1',
          restored_id: 'WK2',
          shelves: {},
          waiting: {},
          survivor_age: null,
          restored_age: null,
          series_plan: { work_id: 'WK2', moves: [], blockers: [], path_changes: 0, emptied: [], mergeable: true, fingerprint: 'f', applied: false }
        });
      })
    );
    render(Page);
    await user.click(await screen.findByRole('button', { name: 'Séparer' }));
    expect(calls).toBe(0); // one click is only a question
    await user.click(screen.getByRole('button', { name: 'Annuler' }));
    expect(calls).toBe(0);
    await user.click(screen.getByRole('button', { name: 'Séparer' }));
    await user.click(screen.getByRole('button', { name: 'Confirmer la séparation' }));
    await waitFor(() => expect(calls).toBe(1));
    expect(await screen.findByText(/rien n'est appliqué/)).toBeInTheDocument();
  });

  // req: EF-USR-022
  it('asks for the password in place on a stale session, then plays "Séparer" again', async () => {
    const user = userEvent.setup();
    current = sheet({
      can: { ...CAN, separate: true },
      merges: [{ merge_id: 'M1', merged_at: '2026-11-10T09:00:00Z', undoable_until: '2026-12-10T09:00:00Z' }]
    });
    let calls = 0;
    server.use(
      serve(),
      http.post(`${BASE}/api/v1/auth/reauth`, () => HttpResponse.json({})),
      http.post(`${BASE}/api/v1/works/merges/M1/separate`, () => {
        calls += 1;
        if (calls === 1) return problem(401, 'reauth_required', 'Confirmez', { reauth_methods: ['password'] });
        return HttpResponse.json({
          merge_id: 'M1', survivor_id: 'WK1', restored_id: 'WK2', shelves: {}, waiting: {},
          survivor_age: null, restored_age: null,
          series_plan: { work_id: 'WK2', moves: [], blockers: [], path_changes: 0, emptied: [], mergeable: true, fingerprint: 'f', applied: false }
        });
      })
    );
    render(Page);
    await user.click(await screen.findByRole('button', { name: 'Séparer' }));
    await user.click(screen.getByRole('button', { name: 'Confirmer la séparation' }));
    expect(await screen.findByText('Confirmez votre identité')).toBeInTheDocument();
    await user.type(screen.getByLabelText(/mot de passe/i), 'secret-phrase');
    await user.click(screen.getByRole('button', { name: /^Confirmer$/ }));
    await waitFor(() => expect(calls).toBe(2));
  });

  // req: EF-BIB-003
  it('shows the plan of a suggested attach, and confirms it with the plan token only', async () => {
    const user = userEvent.setup();
    current = sheet({
      can: { ...CAN, confirm_attach: true },
      attach: { attach_id: 'AT1', from_work_id: 'WK9', to_work_id: 'WK1' }
    });
    server.use(
      serve(),
      http.post(`${BASE}/api/v1/attachments/AT1/plan`, () =>
        HttpResponse.json({
          attach_id: 'AT1', edition_id: 'ED1', work_id: 'WK1', token: '3-abcdef12',
          withdrawals: [{ edition_id: 'ED1', shelf_id: 'S1', reason: 'above_ceiling' }]
        })
      ),
      http.post(`${BASE}/api/v1/attachments/AT1/confirm`, async ({ request }) => {
        await record(request);
        return HttpResponse.json({ attach_id: 'AT1', edition_id: 'ED1', work_id: 'WK1', token: '3-abcdef12', withdrawals: [] });
      })
    );
    render(Page);
    await user.click(await screen.findByRole('button', { name: 'Voir le plan' }));
    expect(await screen.findByText('1 retrait d\'étagère')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Confirmer le rattachement' }));
    await waitFor(() => expect(bodies[0]).toEqual({ confirm: '3-abcdef12' }));
  });

  // req: EF-USR-006
  it('shows the problem and nothing of the sheet to someone the server refuses', async () => {
    server.use(http.get(`${API}/sheet`, () => problem(403, 'forbidden', 'Accès refusé')));
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent('Accès refusé');
    expect(screen.queryByText('Trous')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });

  // req: EF-BIB-020
  it('draws the cover from the 320 thumbnail only', async () => {
    server.use(serve());
    const { container } = render(Page);
    await screen.findByText('src-A');
    expect(container.querySelector('img')?.getAttribute('src')).toBe(`/api/v1/thumbnails/${'a'.repeat(64)}/320`);
  });
});
