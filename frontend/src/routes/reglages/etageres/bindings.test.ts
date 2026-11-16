import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import Page from './+page.svelte';

// The shelves screen, bindings side (EF-EXP-002, EF-EXP-047, L33a): a shelf takes N destinations,
// each with its server, its transport and what it keeps apart; a new one is linked by choosing a
// server, and a refusal says its reason (the problem the server wrote: title, cause, action).
// `GET|POST /api/v1/shelves/{id}/destinations` and `GET /api/v1/targets`.
const BASE = 'http://shelfhold.test';
const server = setupServer();
const FAMILY = '01JSHLF000000000000000000F';
const ADULT = '01JSHLF000000000000000000A';
const SERVER = '01JTARG000000000000000000A';
const LOOSE = '01JTARG000000000000000000B';
const BIND = (shelf: string) => `${BASE}/api/v1/shelves/${shelf}/destinations`;
const binding = (over: Record<string, unknown> = {}) => ({
  id: '01JDEST000000000000000000A',
  target_id: SERVER,
  transport: 'tree',
  tree_id: '01JTREE000000000000000000A',
  role: 'primary',
  partitioning: 'library',
  present_units: null,
  ...over
});
const problem = (code: string, status: number, extra: Record<string, unknown> = {}) =>
  HttpResponse.json(
    {
      type: `/api/v1/problems/${code}`, status, code,
      title: `title of ${code}`, cause: `cause of ${code}`, action: `action of ${code}`, ...extra
    },
    { status }
  );

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

const world = (bindings: Record<string, unknown[]>) =>
  server.use(
    http.get(`${BASE}/api/v1/shelves`, () =>
      HttpResponse.json({
        items: [
          { id: FAMILY, name: 'Famille', max_age_rating: 13 },
          { id: ADULT, name: 'Adultes', max_age_rating: null }
        ],
        since: 'v1', removed: []
      })
    ),
    http.get(`${BASE}/api/v1/destinations`, () => HttpResponse.json({ items: [] })),
    http.get(`${BASE}/api/v1/targets`, () =>
      HttpResponse.json({
        items: [
          { id: SERVER, name: 'Kavita maison', kind: 'kavita', partitioning: 'library' },
          { id: LOOSE, name: 'Dossier partagé', kind: 'folder', partitioning: 'none' }
        ]
      })
    ),
    http.get(BIND(FAMILY), () => HttpResponse.json({ destinations: bindings[FAMILY] ?? [] })),
    http.get(BIND(ADULT), () => HttpResponse.json({ destinations: bindings[ADULT] ?? [] }))
  );
const section = async (name: string) =>
  (await screen.findByRole('heading', { name: `Destinations de « ${name} »`, level: 2 })).closest(
    'section'
  ) as HTMLElement;

describe('Étagères: les destinations d’une étagère', () => {
  // req: EF-EXP-002
  it('one shelf shows N destinations, each with its server, transport and partitioning', async () => {
    world({
      [ADULT]: [
        binding(),
        binding({ id: '01JDEST000000000000000000B', target_id: LOOSE, role: 'secondary', partitioning: 'none' })
      ]
    });
    render(Page);
    const adults = await section('Adultes');
    const items = await within(adults).findAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('Kavita maison');
    expect(items[0]).toHaveTextContent('Cloisonne par bibliothèque');
    expect(items[1]).toHaveTextContent('Dossier partagé');
    expect(items[1]).toHaveTextContent('Ne cloisonne pas');
    expect(items[1]).toHaveTextContent('Copie suivie');
    expect(within(await section('Famille')).queryAllByRole('listitem')).toHaveLength(0);
  });

  // req: EF-EXP-002
  it('links a new destination: the chosen server, the transport, the folder, as the route takes them', async () => {
    world({});
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post(BIND(ADULT), async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: '01JDEST000000000000000000C' }, { status: 201 });
      })
    );
    const user = userEvent.setup();
    render(Page);
    const adults = await section('Adultes');
    await user.selectOptions(within(adults).getByLabelText('Serveur'), 'Kavita maison');
    await user.type(within(adults).getByLabelText('Dossier'), 'manga');
    await user.click(within(adults).getByRole('button', { name: 'Lier la destination' }));
    expect(await within(adults).findByRole('status')).toHaveTextContent('Destination liée');
    expect(body).toEqual({ target_id: SERVER, transport: 'tree', root_subpath: 'manga' });
  });

  // req: EF-EXP-047
  it('test_family_shelf_destination_refusal_shows_reason: the title, the cause and the action of the refusal', async () => {
    world({});
    server.use(http.post(BIND(FAMILY), () => problem('shelf.destination_not_partitioned', 409)));
    const user = userEvent.setup();
    render(Page);
    const family = await section('Famille');
    await user.selectOptions(within(family).getByLabelText('Serveur'), 'Dossier partagé');
    await user.click(within(family).getByRole('button', { name: 'Lier la destination' }));
    const alert = await within(family).findByRole('alert');
    expect(alert).toHaveTextContent('title of shelf.destination_not_partitioned');
    expect(alert).toHaveTextContent('cause of shelf.destination_not_partitioned');
    expect(alert).toHaveTextContent('action of shelf.destination_not_partitioned');
    expect(alert).toHaveTextContent('shelf.destination_not_partitioned'); // the stable code
    // the refusal changes nothing: the shelf lists what it listed, the other shelf is untouched
    expect(within(family).queryAllByRole('listitem')).toHaveLength(0);
    expect(within(await section('Adultes')).queryByRole('alert')).toBeNull();
  });

  // req: EF-EXP-047
  it('a family shelf says it is one, and says it before the choice of a server that does not partition', async () => {
    world({});
    render(Page);
    const family = await section('Famille');
    expect(family).toHaveTextContent('Étagère famille');
    expect(within(await section('Adultes')).queryByText('Étagère famille')).toBeNull();
  });

  // req: EF-USR-022
  it('asks again for a fresh session in place, then replays the same link', async () => {
    world({});
    let calls = 0;
    server.use(
      http.post(BIND(ADULT), () => {
        calls += 1;
        return calls === 1
          ? problem('reauth_required', 401, { reauth_methods: ['password'] })
          : HttpResponse.json({ id: '01JDEST000000000000000000C' }, { status: 201 });
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, () => HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' }))
    );
    const user = userEvent.setup();
    render(Page);
    const adults = await section('Adultes');
    await user.selectOptions(within(adults).getByLabelText('Serveur'), 'Kavita maison');
    await user.click(within(adults).getByRole('button', { name: 'Lier la destination' }));
    const prompt = await screen.findByRole('region', { name: 'Confirmez votre identité' });
    await user.type(within(prompt).getByLabelText('Mot de passe'), 'x'.repeat(12));
    await user.click(within(prompt).getByRole('button', { name: 'Confirmer' }));
    expect(await within(adults).findByRole('status')).toHaveTextContent('Destination liée');
    expect(calls).toBe(2);
  });

  // req: EF-EXP-002
  it('the screen is in English when the interface is', async () => {
    setUiLanguage('en');
    world({ [ADULT]: [binding()] });
    render(Page);
    const adults = await screen.findByRole('heading', { name: 'Destinations of “Adultes”', level: 2 });
    expect(adults.closest('section')).toHaveTextContent('Keeps accounts apart by library');
  });
});
