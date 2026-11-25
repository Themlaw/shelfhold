import { render, screen, waitFor, within } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const KAVITA = 'http://kavita.test:5000';
const KOMGA = 'http://komga.test:5000';

type Listener = (message: MessageEvent<string>) => void;
const FRAME = JSON.stringify({
  type: 'ops.request_arrived',
  visibility: 'household',
  payload: {},
  occurred_at: '2026-11-03T09:00:00Z',
  title: null,
  title_masked: false
});

class FakeSource {
  static all: FakeSource[] = [];
  listeners = new Map<string, Listener[]>();
  closed = false;
  onerror: ((event: Event) => void) | null = null;
  constructor(public url: string) {
    FakeSource.all.push(this);
  }
  addEventListener(name: string, listener: Listener) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]);
  }
  close() {
    this.closed = true;
  }
  emit(name: string, data = FRAME) {
    for (const listener of this.listeners.get(name) ?? []) listener({ data } as MessageEvent<string>);
  }
}

let works: unknown[] = [];
let requests: unknown[] = [];
let worksQuery: URLSearchParams | null = null;
let requestCalls = 0;
let resume: unknown[] = [];
let resumeQuery: URLSearchParams | null = null;

const request = (id: string, state: string, family: string, open: string | null = null) => ({
  id,
  state,
  title: `Titre ${id}`,
  reject_reason: null,
  created_at: '2026-11-03T09:00:00Z',
  updated_at: '2026-11-03T09:00:00Z',
  family_state: family,
  open_url: open
});

const server = setupServer(
  http.get(`${BASE}/healthz`, () => HttpResponse.json({ state: 'ok' })),
  http.get(`${BASE}/api/v1/works`, ({ request: r }) => {
    worksQuery = new URL(r.url).searchParams;
    return HttpResponse.json({ items: works, total: works.length, since: 't', removed: [] });
  }),
  http.get(`${BASE}/api/reader/continue`, ({ request: r }) => {
    resumeQuery = new URL(r.url).searchParams;
    return HttpResponse.json({ items: resume, open_urls: { kavita: KAVITA } });
  }),
  http.get(`${BASE}/api/v1/requests`, () => {
    requestCalls += 1;
    return HttpResponse.json({ items: requests });
  })
);
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  works = [];
  requests = [];
  worksQuery = null;
  requestCalls = 0;
  resume = [];
  resumeQuery = null;
  FakeSource.all = [];
  vi.stubGlobal('EventSource', FakeSource);
});
afterEach(() => {
  server.resetHandlers();
  vi.unstubAllGlobals();
});
afterAll(() => server.close());

describe('home page', () => {
  // req: EF-UX-005
  it('greets the person signed in with one level-one heading, and says nothing of a pre-alpha', async () => {
    render(Page, { data: { user: { name: 'Antoine', role: 'member' } } });
    expect(screen.getByRole('heading', { level: 1, name: 'Bonjour, Antoine.' })).toBeInTheDocument();
    await screen.findByRole('link', { name: 'Ajouter un premier livre' });
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(document.body.textContent).not.toMatch(/pré-alpha|pre-alpha/);
  });

  // req: ENF-OPS-003
  it('calls /healthz through the generated client (the instance panel of an administrator)', async () => {
    server.use(http.get(`${BASE}/api/v1/requests/queue`, () => HttpResponse.json({ items: [] })));
    render(Page, { data: { user: { name: 'Claire', role: 'admin' } } });
    expect(await screen.findByText('Le serveur répond.')).toBeInTheDocument();
  });

  // req: EF-UX-010
  it('an administrator sees what waits for approval, the count said in words', async () => {
    server.use(
      http.get(`${BASE}/api/v1/requests/queue`, () =>
        HttpResponse.json({
          items: [
            { id: 'q1', title: 'Le Jardin des lucioles', requester_role: 'child', requester_id: 'p', created_at: '2026-11-25T09:00:00Z', needs_age: true, target_kind: 'work', visibility: 'household' },
            { id: 'q2', title: null, requester_role: 'member', requester_id: 'p2', created_at: '2026-11-24T09:00:00Z', needs_age: false, target_kind: 'work', visibility: 'private' }
          ]
        })
      )
    );
    render(Page, { data: { user: { name: 'Claire', role: 'admin' } } });
    const section = await screen.findByRole('region', { name: /À approuver/ });
    const rows = within(section).getAllByRole('listitem');
    expect(rows.map((r) => r.querySelector('.entry__title')?.textContent)).toEqual(['Le Jardin des lucioles', 'Titre masqué']);
    expect(within(section).getByText('2', { selector: '.badge *, .badge' })).toBeInTheDocument();
    expect(screen.getByText('2 demandes attendent votre accord.')).toBeInTheDocument();
  });

  // req: EF-UX-010
  it('a member is never asked for the approval queue', async () => {
    let asked = false;
    server.use(http.get(`${BASE}/api/v1/requests/queue`, () => ((asked = true), HttpResponse.json({ items: [] }))));
    render(Page, { data: { user: { name: 'Antoine', role: 'member' } } });
    await screen.findByRole('link', { name: 'Ajouter un premier livre' });
    expect(asked).toBe(false);
    expect(screen.queryByRole('region', { name: /À approuver/ })).toBeNull();
  });

  // req: EF-UX-003
  it('a list the server could not give is said in its block; the rest of the page stands', async () => {
    server.use(http.get(`${BASE}/api/v1/works`, () => HttpResponse.json({ title: 'x', status: 500 }, { status: 500 })));
    requests = [request('b', 'approved', 'preparing')];
    render(Page);
    const recent = await screen.findByRole('region', { name: 'Arrivé récemment' });
    expect(await within(recent).findByText(/n’a pas pu être lue/)).toBeInTheDocument();
    expect(within(recent).getByRole('button', { name: 'Réessayer' })).toBeInTheDocument();
    expect(await screen.findByText('En préparation')).toBeInTheDocument();
  });

  // req: EF-UX-008
  it('test_empty_states_action_oriented', async () => {
    render(Page);
    const add = await screen.findByRole('link', { name: 'Ajouter un premier livre' });
    expect(add).toHaveAttribute('href', '/chercher');
    // "Mes demandes" is not a dead end either
    const mine = screen.getByRole('region', { name: 'Mes demandes' });
    expect(within(mine).getByRole('link', { name: 'Chercher un livre' })).toHaveAttribute(
      'href',
      '/chercher'
    );
  });

  // req: EF-UX-005
  it('test_home_empty_state_gives_no_clue_about_what_the_server_hides', async () => {
    // a child whose ceiling hides every Work is served the page of an empty library, and the
    // screen has no word for "hidden": the same title, the same action, nothing else
    const { container } = render(Page);
    await screen.findByRole('link', { name: 'Ajouter un premier livre' });
    const section = container.querySelector('.recently-arrived');
    expect(section?.textContent?.split(/\s+/).join(' ').trim()).toBe(
      'Arrivé récemment Bibliothèque vide Ajouter un premier livre'
    );
    expect(container.textContent ?? '').not.toMatch(/mask|masqu|hidden|filter|filtr/i);
  });

  // req: EF-UX-005
  it('test_home_recently_arrived_reads_the_filtered_list_newest_first', async () => {
    works = [
      { work_id: 'w2', title: 'Plus récent', kind: 'manga' },
      { work_id: 'w1', title: 'Plus ancien', kind: 'manga' }
    ];
    render(Page);
    const section = await screen.findByRole('region', { name: 'Arrivé récemment' });
    const titles = (await within(section).findAllByRole('listitem')).map((li) => within(li).getByRole('heading').textContent?.trim());
    expect(titles).toEqual(['Plus récent', 'Plus ancien']);
    expect(worksQuery?.get('sort')).toBe('recent');
    expect(screen.queryByRole('link', { name: 'Ajouter un premier livre' })).toBeNull();
  });

  // req: EF-LEC-008
  it('test_continue_reading_home', async () => {
    resume = [
      { work_id: 'w1', title: 'Série A', unit_id: 'u2', number: '2', unit_title: null, state: 'next', target: 'kept_copy', approximate: true, carrier_type: 'content', carrier_id: 'c2' },
      { work_id: 'w2', title: 'Série B', unit_id: 'u9', number: '9', unit_title: null, state: 'in_progress', target: 'kavita', approximate: false, carrier_type: null, carrier_id: null, open_url: KAVITA }
    ];
    render(Page);
    // the first entry is the one the reader left last: « Vous en étiez là », the others under it
    const hero = await screen.findByRole('region', { name: 'Vous en étiez là' });
    expect(hero).toHaveTextContent('Série A');
    expect(hero).toHaveTextContent('N° 2 · prochaine à lire · environ');
    expect(within(hero).getByRole('link', { name: 'Lire, Série A, n° 2' })).toHaveAttribute('href', '/lire/content/c2');
    const also = screen.getByRole('region', { name: 'Aussi en cours' });
    const rows = within(also).getAllByRole('listitem');
    expect(rows.map((r) => r.querySelector('.resume__text')?.textContent?.split(/\s+/).join(' ').trim())).toEqual([
      'Série B N° 9 · en cours Ouvrir dans Kavita'
    ]);
    expect(within(rows[0]!).getByRole('link', { name: 'Ouvrir dans Kavita, Série B, n° 9' })).toHaveAttribute('href', KAVITA);
    expect(resumeQuery?.get('limit')).toBe('6'); // a bounded list asked for
  });

  // req: EF-LEC-008
  it('test_continue_reading_label_names_destination_type', async () => {
    // the target carries the type of the destination: a Komga server is not Kavita
    server.use(
      http.get(`${BASE}/api/reader/continue`, () =>
        HttpResponse.json({
          items: [
            { work_id: 'w3', title: 'Série C', unit_id: 'u3', number: '3', unit_title: null, state: 'in_progress', target: 'komga', approximate: false, carrier_type: null, carrier_id: null, open_url: KOMGA }
          ],
          open_urls: { komga: KOMGA }
        })
      )
    );
    render(Page);
    const hero = await screen.findByRole('region', { name: 'Vous en étiez là' });
    expect(hero).toHaveTextContent('N° 3 · en cours');
    const link = within(hero).getByRole('link', { name: 'Ouvrir dans Komga, Série C, n° 3' });
    expect(link).toHaveAttribute('href', KOMGA);
    expect(link).toHaveTextContent('Ouvrir dans Komga');
  });

  // req: EF-LEC-008
  it('test_continue_reading_two_servers_of_one_kind_each_entry_opens_its_own', async () => {
    // two Kavita servers, an account on each: each entry carries the address of its own server —
    // a map keyed by the kind could hand the second series the first server's address
    server.use(
      http.get(`${BASE}/api/reader/continue`, () =>
        HttpResponse.json({
          items: [
            { work_id: 'w4', title: 'Série D', unit_id: 'u4', number: '4', unit_title: null, state: 'in_progress', target: 'kavita', approximate: false, carrier_type: null, carrier_id: null, open_url: 'http://kavita-un.test:5000' },
            { work_id: 'w5', title: 'Série E', unit_id: 'u5', number: '5', unit_title: null, state: 'in_progress', target: 'kavita', approximate: false, carrier_type: null, carrier_id: null, open_url: 'http://kavita-deux.test:5000' }
          ],
          open_urls: { kavita: 'http://kavita-un.test:5000', 't-2': 'http://kavita-deux.test:5000' }
        })
      )
    );
    render(Page);
    const hero = await screen.findByRole('region', { name: 'Vous en étiez là' });
    expect(within(hero).getByRole('link', { name: 'Ouvrir dans Kavita, Série D, n° 4' })).toHaveAttribute(
      'href',
      'http://kavita-un.test:5000'
    );
    const also = screen.getByRole('region', { name: 'Aussi en cours' });
    expect(within(also).getByRole('link', { name: 'Ouvrir dans Kavita, Série E, n° 5' })).toHaveAttribute(
      'href',
      'http://kavita-deux.test:5000'
    );
  });

  // req: EF-LEC-008
  it('test_continue_reading_empty_leaves_no_trace_on_the_home', async () => {
    // nothing under way, or a child with everything hidden: no section, no word, no empty state
    render(Page);
    await screen.findByRole('link', { name: 'Ajouter un premier livre' });
    expect(screen.queryByRole('region', { name: 'Vous en étiez là' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Aussi en cours' })).toBeNull();
    expect(document.body.textContent ?? '').not.toMatch(/en étiez là|Reprendre|Continue reading/);
  });

  // req: EF-LEC-008
  it('test_continue_reading_refuses_a_link_that_is_not_a_web_address', async () => {
    server.use(
      http.get(`${BASE}/api/reader/continue`, () =>
        HttpResponse.json({
          items: [{ work_id: 'w2', title: 'Série B', unit_id: 'u9', number: '9', unit_title: null, state: 'in_progress', target: 'kavita', approximate: false, carrier_type: null, carrier_id: null, open_url: 'javascript:alert(1)' }],
          open_urls: { kavita: 'javascript:alert(1)' }
        })
      )
    );
    render(Page);
    const hero = await screen.findByRole('region', { name: 'Vous en étiez là' });
    expect(within(hero).queryByRole('link')).toBeNull();
  });

  // req: EF-UX-010
  it('test_home_my_requests_one_state_with_open_in_kavita', async () => {
    requests = [
      request('a', 'arrived', 'available', KAVITA),
      request('b', 'approved', 'preparing'),
      request('c', 'pending', 'waiting')
    ];
    render(Page);
    const section = await screen.findByRole('region', { name: 'Mes demandes' });
    const rows = await within(section).findAllByRole('listitem');
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining('Disponible dans Kavita'),
      expect.stringContaining('En préparation'),
      expect.stringContaining('En attente')
    ]);
    expect(within(rows[0]!).getByRole('link', { name: 'Ouvrir dans Kavita' })).toHaveAttribute(
      'href',
      KAVITA
    );
    for (const row of rows.slice(1)) expect(within(row).queryByRole('link')).toBeNull();
    // the raw step is nobody's business here
    expect(section.textContent).not.toMatch(/arrived|approved|pending/);
  });

  // req: EF-UX-010
  it('test_home_refuses_a_link_that_is_not_a_web_address', async () => {
    requests = [request('a', 'arrived', 'available', 'javascript:alert(1)')];
    render(Page);
    const section = await screen.findByRole('region', { name: 'Mes demandes' });
    await within(section).findByText('Disponible dans Kavita');
    expect(within(section).queryByRole('link', { name: 'Ouvrir dans Kavita' })).toBeNull();
  });

  // req: EF-UX-010
  it('test_home_updates_by_the_one_event_stream', async () => {
    requests = [request('a', 'approved', 'preparing')];
    const { unmount } = render(Page);
    await screen.findByText('En préparation');
    // one stream, the typed one of L12: no second door, no polling
    expect(FakeSource.all.map((s) => s.url)).toEqual(['/api/v1/events']);
    const calls = requestCalls;
    requests = [request('a', 'arrived', 'available', KAVITA)];
    FakeSource.all[0]!.emit('ops.request_arrived');
    expect(await screen.findByText('Disponible dans Kavita')).toBeInTheDocument();
    expect(requestCalls).toBeGreaterThan(calls);
    // a resync reloads, then reopens the stream at the head
    FakeSource.all[0]!.emit('resync_required');
    await waitFor(() => expect(FakeSource.all).toHaveLength(2));
    unmount();
    expect(FakeSource.all.every((s) => s.closed)).toBe(true);
  });

  // req: EF-UX-010
  it('test_home_a_server_refusal_shows_the_empty_states_not_a_crash', async () => {
    server.use(
      http.get(`${BASE}/api/v1/works`, () => HttpResponse.json({ title: 'x', status: 401 }, { status: 401 })),
      http.get(`${BASE}/api/v1/requests`, () => HttpResponse.json({ title: 'x', status: 401 }, { status: 401 }))
    );
    render(Page);
    expect(await screen.findByRole('link', { name: 'Ajouter un premier livre' })).toBeInTheDocument();
  });
});
