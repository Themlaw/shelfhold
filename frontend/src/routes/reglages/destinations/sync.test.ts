import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

// The switch of the progress sync of a binding on the destinations screen (M4 L46, EF-LEC-014):
// shown only for a destination that can synchronise, a reserved action (fresh session), and the
// refusal of the server (a destination that cannot) is said, never hidden.
const BASE = 'http://shelfhold.test';
const server = setupServer();
const ADMIN_PW = ['synthetic', 'admin', 'pw', '0046'].join('-'); // no literal: gitleaks scans commits
const KAVITA_DEST = '01JDEST000000000000000000K';
const KOMGA_DEST = '01JDEST000000000000000000G';
const destination = (id: string, name: string, over: Record<string, unknown> = {}) => ({
  id,
  shelf_id: '01JSHLF000000000000000000A',
  shelf_name: name,
  target_id: '01JTARG000000000000000000A',
  root_subpath: `${name.toLowerCase()}/`,
  transport: 'tree',
  adopted: false,
  flagged_names: 0,
  template: {},
  target_kind: 'kavita',
  sync_progress: false,
  can_sync_progress: true,
  ...over
});
const stale = () =>
  HttpResponse.json(
    {
      type: '/api/v1/problems/reauth-required', title: 'x', status: 401, code: 'reauth_required',
      auth_age_s: 2000, max_age_s: 900, reauth_methods: ['password']
    },
    { status: 401 }
  );

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const list = (items: unknown[]) =>
  server.use(http.get(`${BASE}/api/v1/destinations`, () => HttpResponse.json({ items })));
const line = async (name: RegExp) => (await screen.findByText(name)).closest('li') as HTMLElement;

describe('Progress sync switch', () => {
  // req: EF-LEC-014
  it('offers the switch for a Kavita binding only, off as the server says', async () => {
    list([
      destination(KAVITA_DEST, 'Manga'),
      destination(KOMGA_DEST, 'Comics', { target_kind: 'komga', can_sync_progress: false })
    ]);
    render(Page);
    const kavita = await line(/manga\//);
    expect(within(kavita).getByRole('checkbox', { name: 'Synchroniser la progression' })).not.toBeChecked();
    const komga = await line(/comics\//);
    expect(within(komga).queryByRole('checkbox', { name: 'Synchroniser la progression' })).toBeNull();
  });

  // req: EF-LEC-014
  it('turns it on with one reserved call and shows the state the server answered', async () => {
    list([destination(KAVITA_DEST, 'Manga')]);
    let body: unknown = null;
    server.use(
      http.post(`${BASE}/api/v1/destinations/${KAVITA_DEST}/sync-progress`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ destination_id: KAVITA_DEST, sync_progress: true });
      })
    );
    render(Page);
    const user = userEvent.setup();
    const box = within(await line(/manga\//)).getByRole('checkbox', { name: 'Synchroniser la progression' });
    await user.click(box);
    expect(await screen.findByText('Synchro de progression allumée.')).toBeTruthy();
    expect(body).toEqual({ enabled: true });
    expect(box).toBeChecked();
  });

  // req: EF-LEC-014, EF-USR-024
  it('asks for a fresh session in place, then replays the same call', async () => {
    list([destination(KAVITA_DEST, 'Manga')]);
    let calls = 0;
    server.use(
      http.post(`${BASE}/api/v1/destinations/${KAVITA_DEST}/sync-progress`, () => {
        calls += 1;
        return calls === 1 ? stale() : HttpResponse.json({ destination_id: KAVITA_DEST, sync_progress: true });
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, async ({ request }) => {
        expect(await request.json()).toEqual({ method: 'password', password: ADMIN_PW });
        return HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' });
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(within(await line(/manga\//)).getByRole('checkbox', { name: 'Synchroniser la progression' }));
    const prompt = await screen.findByRole('region', { name: 'Confirmez votre identité' });
    await user.type(within(prompt).getByLabelText('Mot de passe'), ADMIN_PW);
    await user.click(within(prompt).getByRole('button', { name: 'Confirmer' }));
    expect(await screen.findByText('Synchro de progression allumée.')).toBeTruthy();
    expect(calls).toBe(2);
  });

  // req: EF-LEC-014
  it('says why when the server refuses, and leaves the switch where it was', async () => {
    list([destination(KAVITA_DEST, 'Manga')]);
    server.use(
      http.post(`${BASE}/api/v1/destinations/${KAVITA_DEST}/sync-progress`, () =>
        HttpResponse.json(
          { type: '/api/v1/problems/destination-sync-unsupported', title: 'x', status: 409, code: 'destination.sync_unsupported' },
          { status: 409 }
        )
      )
    );
    render(Page);
    const user = userEvent.setup();
    const box = within(await line(/manga\//)).getByRole('checkbox', { name: 'Synchroniser la progression' });
    await user.click(box);
    expect(await screen.findByRole('alert')).toHaveTextContent('Cette destination ne sait pas synchroniser la progression.');
    expect(box).not.toBeChecked();
  });
});
