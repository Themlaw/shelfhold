import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import type { ApprovalOut, QueueItemOut } from '#lib/api/types.gen.ts';
import ApproveSheet from './ApproveSheet.svelte';

const BASE = 'http://shelfhold.test';
const ID = '01JREQ00000000000000000001';
const server = setupServer();

const item = (over: Partial<QueueItemOut> = {}): QueueItemOut => ({
  id: ID,
  title: 'Un livre demandé',
  target_kind: 'source_ref',
  visibility: 'household',
  requester_id: '01JUSER0000000000000000001',
  requester_role: 'child',
  needs_age: true,
  created_at: '2026-11-10T09:00:00Z',
  ...over
});
const preview = (over: Partial<ApprovalOut> = {}): ApprovalOut =>
  ({
    confirmation_required: false,
    current_age: null,
    current_x: false,
    decided_age: null,
    decided_x: false,
    edition_id: null,
    needs_age: true,
    resolved: true,
    shelf_id: null,
    size_bytes: 52_428_800,
    units: 3,
    units_minimum: false,
    ...over
  }) as ApprovalOut;
const problem = (status: number, code: string, extra: Record<string, unknown> = {}) =>
  HttpResponse.json({ type: `/api/v1/problems/${code}`, title: 'x', status, code, ...extra }, { status });

let posted: { body: unknown; csrf: string | null }[] = [];
function answer(view: ApprovalOut, approve?: () => Response) {
  server.use(
    http.get(`${BASE}/api/v1/requests/:id/approval`, () => HttpResponse.json(view)),
    http.post(`${BASE}/api/v1/requests/:id/approve`, async ({ request }) => {
      posted.push({ body: await request.json(), csrf: request.headers.get('x-csrf-token') });
      return approve ? approve() : HttpResponse.json({ id: ID, state: 'approved' });
    })
  );
}

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  posted = [];
  document.cookie = 'csrf=tok123';
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const approveButton = () => screen.getByRole('button', { name: 'Approuver' });

describe('ApproveSheet', () => {
  // req: EF-USR-010
  it('test_approve_sheet_requires_age_for_child_request', async () => {
    answer(preview());
    const onapproved = vi.fn();
    render(ApproveSheet, { item: item(), onapproved });
    const group = await screen.findByRole('group', { name: 'Âge pour cette demande' });
    expect(approveButton()).toBeDisabled();
    const user = userEvent.setup();
    // a click on a disabled button asks the server for nothing
    await user.click(approveButton());
    expect(posted).toEqual([]);
    await user.click(within(group).getByRole('radio', { name: '13+' }));
    expect(approveButton()).toBeEnabled();
    await user.click(approveButton());
    await vi.waitFor(() => expect(onapproved).toHaveBeenCalledOnce());
    expect(posted).toEqual([
      { body: { age: 13, x: false, confirm_size: false, shelf_id: null }, csrf: 'tok123' }
    ]);
  });

  // req: EF-USR-010
  it('sends the age 0+ as 0, never as no age', async () => {
    answer(preview());
    render(ApproveSheet, { item: item(), onapproved: vi.fn() });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('radio', { name: 'Tous publics' }));
    await user.click(approveButton());
    await vi.waitFor(() => expect(posted).toHaveLength(1));
    expect((posted[0]?.body as { age: number }).age).toBe(0);
  });

  // req: EF-USR-010
  it('sends X as the top tier with the X flag, the way the server reads it', async () => {
    answer(preview());
    render(ApproveSheet, { item: item(), onapproved: vi.fn() });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('radio', { name: 'X' }));
    await user.click(approveButton());
    await vi.waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]?.body).toMatchObject({ age: 18, x: true });
  });

  // req: EF-USR-010
  it('prefills the age already decided for this Work, and still lets it be changed', async () => {
    answer(preview({ decided_age: 12 }));
    render(ApproveSheet, { item: item(), onapproved: vi.fn() });
    expect(await screen.findByRole('radio', { name: '12+' })).toBeChecked();
    expect(approveButton()).toBeEnabled();
  });

  // req: EF-USR-010
  it('an adult request is approved in one gesture, with no age asked', async () => {
    answer(preview({ needs_age: false }));
    const onapproved = vi.fn();
    render(ApproveSheet, { item: item({ needs_age: false, requester_role: 'member' }), onapproved });
    await screen.findByText(/3\sunités/);
    expect(screen.queryByRole('group')).toBeNull();
    await userEvent.setup().click(approveButton());
    await vi.waitFor(() => expect(onapproved).toHaveBeenCalledOnce());
    expect(posted[0]?.body).toMatchObject({ age: null, x: false, confirm_size: false });
  });

  // req: EF-USR-010
  it('shows the size summary, and says when more will follow', async () => {
    answer(preview({ needs_age: false, units: 1, size_bytes: 1000, units_minimum: true }));
    render(ApproveSheet, { item: item({ needs_age: false }), onapproved: vi.fn() });
    expect(await screen.findByText(/1\sunité,\s1\s000\soctets/)).toBeInTheDocument();
    expect(screen.getByText(/d’autres suivront|d'autres suivront/)).toBeInTheDocument();
  });

  // req: EF-USR-010
  it('over the threshold, approving needs the size to be confirmed first', async () => {
    answer(preview({ needs_age: false, units: 150, confirmation_required: true }));
    render(ApproveSheet, { item: item({ needs_age: false }), onapproved: vi.fn() });
    const confirm = await screen.findByRole('checkbox', { name: /la taille/ });
    expect(approveButton()).toBeDisabled();
    const user = userEvent.setup();
    await user.click(confirm);
    await user.click(approveButton());
    await vi.waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]?.body).toMatchObject({ confirm_size: true });
  });

  // req: EF-USR-010
  it('a masked title is said to be masked: no id and no title is made up', async () => {
    answer(preview({ needs_age: false }));
    render(ApproveSheet, {
      item: item({ title: null, visibility: 'private', requester_role: 'member', needs_age: false }),
      onapproved: vi.fn()
    });
    expect(await screen.findByText('Titre masqué')).toBeInTheDocument();
    expect(screen.queryByText(ID)).toBeNull();
  });

  // req: EF-USR-010
  it('a refusal of the server is shown and the request is not reported as approved', async () => {
    answer(preview(), () => problem(422, 'request.choose_age', { title: 'Choisissez un âge' }));
    const onapproved = vi.fn();
    render(ApproveSheet, { item: item(), onapproved });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('radio', { name: '10+' }));
    await user.click(approveButton());
    expect(await screen.findByRole('alert')).toHaveTextContent('Choisissez un âge');
    expect(onapproved).not.toHaveBeenCalled();
  });

  // req: EF-USR-010
  it('the reserved action asks for the password in place, then approves with the same choice', async () => {
    let first = true;
    answer(preview(), () => {
      if (first) {
        first = false;
        return problem(401, 'reauth_required', { reauth_methods: ['password'] });
      }
      return HttpResponse.json({ id: ID, state: 'approved' });
    });
    server.use(http.post(`${BASE}/api/v1/auth/reauth`, () => HttpResponse.json({})));
    const onapproved = vi.fn();
    render(ApproveSheet, { item: item(), onapproved });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('radio', { name: '16+' }));
    await user.click(approveButton());
    expect(await screen.findByText('Confirmez votre identité')).toBeInTheDocument();
    expect(onapproved).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText('Mot de passe'), 'pw');
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    await vi.waitFor(() => expect(onapproved).toHaveBeenCalledOnce());
    expect(posted.map((p) => p.body)).toEqual([
      expect.objectContaining({ age: 16 }),
      expect.objectContaining({ age: 16 })
    ]);
  });

  // req: EF-USR-010
  it('a preview that fails shows the problem and offers no approval', async () => {
    server.use(
      http.get(`${BASE}/api/v1/requests/:id/approval`, () => problem(403, 'forbidden', { title: 'Interdit' }))
    );
    render(ApproveSheet, { item: item(), onapproved: vi.fn() });
    expect(await screen.findByRole('alert')).toHaveTextContent('Interdit');
    expect(screen.queryByRole('button', { name: 'Approuver' })).toBeNull();
  });
});
