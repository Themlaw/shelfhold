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
    return HttpResponse.json({ items: resume, kavita_url: KAVITA });
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
  it('has the pre-alpha title as level-one heading', async () => {
    render(Page);
    expect(screen.getByRole('heading', { level: 1, name: 'Shelfhold — pré-alpha' })).toBeInTheDocument();
    await screen.findByText(/Le serveur répond/);
  });

  // req: ENF-OPS-003
  it('calls /healthz through the generated client', async () => {
    render(Page);
    expect(await screen.findByText('Le serveur répond.')).toBeInTheDocument();
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
    const titles = (await within(section).findAllByRole('listitem')).map((li) => li.textContent);
    expect(titles).toEqual(['Plus récent', 'Plus ancien']);
    expect(worksQuery?.get('sort')).toBe('recent');
    expect(screen.queryByRole('link', { name: 'Ajouter un premier livre' })).toBeNull();
  });

  // req: EF-LEC-008
  it('test_continue_reading_home', async () => {
    resume = [
      { work_id: 'w1', title: 'Série A', unit_id: 'u2', number: '2', unit_title: null, state: 'next', target: 'kept_copy', approximate: true, carrier_type: 'content', carrier_id: 'c2' },
      { work_id: 'w2', title: 'Série B', unit_id: 'u9', number: '9', unit_title: null, state: 'in_progress', target: 'kavita', approximate: false, carrier_type: null, carrier_id: null }
    ];
    render(Page);
    const section = await screen.findByRole('region', { name: 'Reprendre la lecture' });
    const rows = within(section).getAllByRole('listitem');
    expect(rows.map((r) => r.textContent?.split(/\s+/).join(' ').trim())).toEqual([
      'Série A N° 2 · prochaine à lire · environ Lire',
      'Série B N° 9 · en cours Ouvrir dans Kavita'
    ]);
    expect(within(rows[0]!).getByRole('link', { name: 'Lire, Série A, n° 2' })).toHaveAttribute('href', '/lire/content/c2');
    expect(within(rows[1]!).getByRole('link', { name: 'Ouvrir dans Kavita, Série B, n° 9' })).toHaveAttribute('href', KAVITA);
    expect(resumeQuery?.get('limit')).toBe('6'); // a bounded list asked for
  });

  // req: EF-LEC-008
  it('test_continue_reading_label_names_destination_type', async () => {
    // the target carries the type of the destination: a Komga server is not Kavita
    server.use(
      http.get(`${BASE}/api/reader/continue`, () =>
        HttpResponse.json({
          items: [
            { work_id: 'w3', title: 'Série C', unit_id: 'u3', number: '3', unit_title: null, state: 'in_progress', target: 'komga', approximate: false, carrier_type: null, carrier_id: null }
          ],
          open_urls: { komga: KOMGA }
        })
      )
    );
    render(Page);
    const section = await screen.findByRole('region', { name: 'Reprendre la lecture' });
    const row = within(section).getByRole('listitem');
    expect(row.textContent?.split(/\s+/).join(' ').trim()).toBe('Série C N° 3 · en cours Ouvrir dans Komga');
    expect(within(row).getByRole('link', { name: 'Ouvrir dans Komga, Série C, n° 3' })).toHaveAttribute('href', KOMGA);
  });

  // req: EF-LEC-008
  it('test_continue_reading_empty_leaves_no_trace_on_the_home', async () => {
    // nothing under way, or a child with everything hidden: no section, no word, no empty state
    render(Page);
    await screen.findByRole('link', { name: 'Ajouter un premier livre' });
    expect(screen.queryByRole('region', { name: 'Reprendre la lecture' })).toBeNull();
    expect(document.body.textContent ?? '').not.toMatch(/Reprendre|Continue reading/);
  });

  // req: EF-LEC-008
  it('test_continue_reading_refuses_a_link_that_is_not_a_web_address', async () => {
    server.use(
      http.get(`${BASE}/api/reader/continue`, () =>
        HttpResponse.json({
          items: [{ work_id: 'w2', title: 'Série B', unit_id: 'u9', number: '9', unit_title: null, state: 'in_progress', target: 'kavita', approximate: false, carrier_type: null, carrier_id: null }],
          kavita_url: 'javascript:alert(1)'
        })
      )
    );
    render(Page);
    const section = await screen.findByRole('region', { name: 'Reprendre la lecture' });
    expect(within(section).queryByRole('link')).toBeNull();
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
