import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const SUGGESTIONS = `${BASE}/api/v1/merge-suggestions/works`;
const EDITIONS = `${BASE}/api/v1/editions`;
const WORKS_MERGE = `${BASE}/api/v1/works/merge`;
const EDITIONS_MERGE = `${BASE}/api/v1/editions/merge`;

const server = setupServer();

const suggestion = { id: 'S1', a: 'WA', b: 'WB', reason: 'suwayomi_migration', state: 'open' };
const editions = {
  items: [
    { edition_id: 'EA', work_id: 'WA', lang: 'fr', title: 'La Voie', sort_key: 'la voie' },
    { edition_id: 'EB', work_id: 'WB', lang: 'fr', title: 'La Voie', sort_key: 'la voie' },
    { edition_id: 'EC', work_id: 'WA', lang: 'fr', title: 'La Voie', sort_key: 'la voie' }
  ]
};
const workPlan = (over: Record<string, unknown> = {}) => ({
  keep_id: 'WA',
  drop_id: 'WB',
  editions: ['EB'],
  blockers: [],
  keep_age: '6+',
  drop_age: null,
  merged_age: '6+',
  unrated_after: false,
  mergeable: true,
  fingerprint: 'fp1234567890abcd',
  applied: false,
  merge_id: null,
  series_plan: {
    work_id: 'WA',
    moves: [],
    blockers: [],
    path_changes: 0,
    emptied: [],
    mergeable: true,
    fingerprint: 'fp',
    applied: false
  },
  ...over
});
const editionPlan = (over: Record<string, unknown> = {}) => ({
  keep_id: 'EA',
  drop_id: 'EB',
  work_id: 'WA',
  units: ['U1'],
  volumes: [],
  bindings: ['B1'],
  blockers: [],
  tombstoned_units: 1,
  tombstoned_ids: 0,
  keep_age: null,
  drop_age: null,
  merged_age: null,
  unrated_after: true,
  mergeable: false,
  fingerprint: 'efp1234567890abc',
  applied: false,
  ...over
});

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
  setUiLanguage('fr');
  server.use(
    http.get(SUGGESTIONS, () => HttpResponse.json({ items: [suggestion] })),
    http.get(EDITIONS, () => HttpResponse.json(editions))
  );
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Même œuvre ?', () => {
  // req: EF-BIB-029
  it('test_same_work_proposal_screen_shows_the_plan_and_applies_for_its_fingerprint', async () => {
    const user = userEvent.setup();
    const seen: Record<string, unknown>[] = [];
    server.use(
      http.post(WORKS_MERGE, async ({ request }) => {
        expect(request.headers.get('x-csrf-token')).toBe('tok123');
        const body = (await request.json()) as Record<string, unknown>;
        seen.push(body);
        return HttpResponse.json(
          body.confirm ? workPlan({ applied: true, merge_id: 'M1' }) : workPlan()
        );
      })
    );
    render(Page);
    // the proposal is listed with its reason and its two Works
    expect(await screen.findByText('Même œuvre ? (migration de source)')).toBeInTheDocument();
    expect(screen.getAllByText(/« La Voie »/).length).toBeGreaterThan(0);
    expect(screen.getByRole('heading', { level: 1, name: /Même œuvre/ })).toBeInTheDocument();
    // open it, then ask for the plan: the M3 door, without a confirmation
    await user.click(screen.getByRole('button', { name: 'Voir le plan' }));
    await user.click(screen.getByRole('button', { name: 'Calculer le plan' }));
    expect(await screen.findByText('1 édition déplacée')).toBeInTheDocument();
    expect(seen[0]).toEqual({ keep_id: 'WA', drop_id: 'WB' });
    // apply: the fingerprint of the plan that was shown goes back
    await user.click(screen.getByRole('button', { name: 'Appliquer la fusion' }));
    await user.click(await screen.findByRole('button', { name: 'Confirmer la fusion' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Fusion appliquée');
    expect(seen[1]).toEqual({ keep_id: 'WA', drop_id: 'WB', confirm: 'fp1234567890abcd' });
  });

  // req: EF-BIB-029
  it('test_same_work_screen_shows_the_edition_plan_with_the_tombstones_consulted', async () => {
    const user = userEvent.setup();
    const seen: Record<string, unknown>[] = [];
    server.use(
      http.post(EDITIONS_MERGE, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        seen.push(body);
        return HttpResponse.json(editionPlan());
      })
    );
    render(Page);
    await user.click(await screen.findByRole('button', { name: 'Voir le plan' }));
    await user.click(screen.getByRole('button', { name: 'Plan de fusion des éditions' }));
    // the M4 door: the plan says what a tombstone covers, and nothing can be applied
    expect(
      await screen.findByText(/1 fichier couvert par une pierre tombale/)
    ).toBeInTheDocument();
    expect(seen[0]).toEqual({ keep_id: 'EA', drop_id: 'EB' });
    expect(
      screen.queryByRole('button', { name: 'Appliquer la fusion des éditions' })
    ).toBeNull();
  });

  // req: EF-BIB-029
  it('test_same_work_screen_says_so_when_nothing_waits', async () => {
    server.use(http.get(SUGGESTIONS, () => HttpResponse.json({ items: [] })));
    render(Page);
    expect(await screen.findByText("Aucune proposition pour l'instant.")).toBeInTheDocument();
  });

  // req: EF-USR-015
  it('test_same_work_screen_asks_for_a_session_on_a_401', async () => {
    server.use(
      http.get(SUGGESTIONS, () =>
        HttpResponse.json(
          { type: 'x', title: 'x', status: 401, code: 'unauthenticated' },
          { status: 401 }
        )
      )
    );
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent('Connexion requise');
  });

  // req: EF-USR-015
  it('test_same_work_screen_refuses_a_non_admin_on_a_403', async () => {
    server.use(
      http.get(SUGGESTIONS, () =>
        HttpResponse.json(
          { type: '/api/v1/problems/forbidden', title: 'x', status: 403, code: 'forbidden' },
          { status: 403 }
        )
      )
    );
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      "Cet écran est réservé à une session d'administration."
    );
  });
});
