import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const UNIT = '01JUNIT0000000000000000001';
const RARE = '01JUNIT0000000000000000002';
const server = setupServer();
const entry = (unit: string, over: Record<string, unknown> = {}) => ({
  deletion_id: '01JDELE0000000000000000001',
  unit_id: unit,
  level: 'delete_content',
  files: 2,
  bytes: 5_000_000,
  deleted_at: '2026-11-10T10:00:00',
  purge_after: '2026-12-10T10:00:00',
  irreplaceable: false,
  ...over
});
const listing = () =>
  http.get(`${BASE}/api/v1/trash`, () =>
    HttpResponse.json({ entries: [entry(UNIT), entry(RARE, { irreplaceable: true })], total_bytes: 10_000_000 })
  );
const problem = (status: number, extra: Record<string, unknown> = {}) =>
  HttpResponse.json({ type: '/api/v1/problems/x', title: 'x', status, code: 'x', ...extra }, { status });

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Corbeille', () => {
  // req: EF-BIB-021, EF-BIB-052
  it('test_trash_screen_empty_now_typed_confirmation', async () => {
    let sent: { typed: string } | null = null;
    let csrf: string | null = null;
    server.use(
      listing(),
      http.post(`${BASE}/api/v1/trash/empty`, async ({ request }) => {
        sent = (await request.json()) as { typed: string };
        csrf = request.headers.get('x-csrf-token');
        return HttpResponse.json({ removed: 2, kept: 0, held_irreplaceable: 1, held_other: 0 });
      })
    );
    render(Page);
    expect(await screen.findByText(UNIT)).toBeInTheDocument();
    const user = userEvent.setup();
    const empty = screen.getByRole('region', { name: 'Vider la corbeille maintenant' });
    const run = within(empty).getByRole('button', { name: 'Vider maintenant' });
    expect(run).toBeDisabled();
    await user.type(within(empty).getByLabelText('Mot de confirmation'), 'empty');
    expect(run).toBeDisabled();
    await user.type(within(empty).getByLabelText('Mot de confirmation'), '-trash');
    expect(run).toBeEnabled();
    await user.click(run);
    expect(await screen.findByText(/1 irremplaçable gardée|1 gardée/)).toBeInTheDocument();
    expect(sent).toEqual({ typed: 'empty-trash' });
    expect(csrf).toBe('tok123');
  });

  // req: EF-BIB-052, EF-USR-022
  it('purges one Unit only for its identifier typed, and asks the password in place', async () => {
    let attempts = 0;
    let sent: { typed: string } | null = null;
    server.use(
      listing(),
      http.post(`${BASE}/api/v1/units/:id/purge`, async ({ request }) => {
        attempts += 1;
        if (attempts === 1) {
          return problem(401, {
            code: 'reauth_required', auth_age_s: 2000, max_age_s: 900, reauth_methods: ['password']
          });
        }
        sent = (await request.json()) as { typed: string };
        return HttpResponse.json({ anchor_id: UNIT, units: [UNIT], complete: true, erased: 1 });
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, () => HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' }))
    );
    render(Page);
    await screen.findByText(UNIT);
    const user = userEvent.setup();
    const row = screen.getByRole('listitem', { name: new RegExp(UNIT) });
    await user.click(within(row).getByRole('button', { name: 'Effacer maintenant' }));
    const confirm = within(row).getByRole('button', { name: 'Effacer pour de bon' });
    expect(confirm).toBeDisabled();
    await user.type(within(row).getByLabelText(/Saisissez l'identifiant/), 'wrong');
    expect(confirm).toBeDisabled();
    await user.clear(within(row).getByLabelText(/Saisissez l'identifiant/));
    await user.type(within(row).getByLabelText(/Saisissez l'identifiant/), UNIT);
    await user.click(confirm);
    await user.type(await screen.findByLabelText('Mot de passe'), 'pw');
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    expect(await screen.findByText(/effacée|effacé/i)).toBeInTheDocument();
    expect(attempts).toBe(2);
    expect(sent).toEqual({ typed: UNIT });
  });

  // req: EF-BIB-021
  it('flags an irreplaceable Unit, shows the term, and never a path', async () => {
    server.use(listing());
    const { container } = render(Page);
    await screen.findByText(UNIT);
    const rare = screen.getByRole('listitem', { name: new RegExp(RARE) });
    expect(within(rare).getByText('Irremplaçable')).toBeInTheDocument();
    expect(within(rare).getByText(/10 déc|2026-12-10|10\/12\/2026/)).toBeInTheDocument();
    expect(container.innerHTML).not.toMatch(/recycle|\/library|\/store/);
  });

  // req: EF-BIB-021, EF-BIB-052
  it('is closed to a member, a child and a key (403) and to no session (401)', async () => {
    server.use(http.get(`${BASE}/api/v1/trash`, () => problem(403, { code: 'forbidden' })));
    const { container } = render(Page);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(container.querySelector('button')).toBeNull();
  });
});
