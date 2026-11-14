import { render, screen, waitFor, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import Page from './+page.svelte';

// The Search screen (EF-UX-006, EF-UX-011, EF-USR-014). The admin sees what the sources found,
// where it looked, and the verbs « Ajouter à ma bibliothèque » and « Demander »; the child sees a
// title and « Demander », nothing else. The screen decides nothing: the shape of the answer says
// which one it is, and it makes no call the person's role has no use for (an unhandled call fails).
const BASE = 'http://shelfhold.test';
const API = `${BASE}/api/v1`;
const KAVITA = 'http://kavita.test:5000/series/7';
const server = setupServer();

const trust = {
  level: 'official',
  lines: [
    { level: 'official', label: 'Maintenue par le projet', current: true },
    { level: 'community', label: 'Écrite par des bénévoles', current: false },
    { level: 'unknown', label: 'Jamais examinée', current: false }
  ]
};
const edition = (source_key: string, remote_id: string, title: string, format: string) => ({
  source_key,
  remote_id,
  title,
  format
});
const group = (id: string, title: string, editions: ReturnType<typeof edition>[], over = {}) => ({
  group_id: id,
  title,
  appended: false,
  editions,
  editions_by_format: Object.fromEntries(
    [...new Set(editions.map((e) => e.format))].map((f) => [f, editions.filter((e) => e.format === f)])
  ),
  external_ids: [],
  ...over
});
const asked = (over: Record<string, unknown> = {}) => [
  { source_key: 'alpha', name: 'Alpha texts', format: 'text', status: 'ok', code: null, returned: 2, trust, ...over },
  { source_key: 'beta', name: 'Beta scans', format: 'images', status: 'error', code: 'source.timeout', returned: 0, trust: null }
];
const federated = (over: Record<string, unknown> = {}) => ({
  items: [
    group('G1', 'Le Hobbit', [edition('alpha', 'a1', 'Le Hobbit', 'text'), edition('beta', 'b1', 'Der Hobbit', 'images')]),
    group('G2', 'Bilbo le Hobbit', [edition('alpha', 'a2', 'Bilbo le Hobbit', 'text')])
  ],
  searched_in: asked(),
  state: 'results',
  action: null,
  formats: ['text', 'images'],
  next_cursor: null,
  ...over
});
const empty = (over: Record<string, unknown> = {}) =>
  federated({ items: [], state: 'empty', action: 'request', formats: [], searched_in: asked({ returned: 0 }), ...over });
const family = (over: Record<string, unknown> = {}) => ({
  items: [
    { title: 'Conte tranquille', action: 'request', request_ref: 'REF1' },
    { title: 'Autre conte', action: 'request', request_ref: 'REF2' }
  ],
  local: [{ work_id: 'W9', title: 'Déjà sur l’étagère', kind: 'book' }],
  state: 'results',
  next_cursor: null,
  ...over
});
const problem = (status: number, code: string, extra: Record<string, unknown> = {}) =>
  HttpResponse.json({ type: `/api/v1/problems/${code}`, title: `Titre ${code}`, status, code, ...extra }, { status });
const requested = (over: Record<string, unknown> = {}) => ({
  id: 'R1',
  state: 'pending',
  title: 'zzz',
  reject_reason: null,
  created_at: '2026-11-10T09:00:00Z',
  updated_at: '2026-11-10T09:00:00Z',
  family_state: 'waiting',
  open_url: null,
  ...over
});

let searches: URL[] = [];
let posted: { body: unknown; csrf: string | null }[] = [];
function answerSearch(make: (url: URL) => Response | Promise<Response>) {
  server.use(
    http.get(`${API}/search`, ({ request }) => {
      const url = new URL(request.url);
      searches.push(url);
      return make(url);
    })
  );
}
const answerRequests = (make: () => Response = () => HttpResponse.json(requested(), { status: 201 })) =>
  server.use(
    http.post(`${API}/requests`, async ({ request }) => {
      posted.push({ body: await request.json(), csrf: request.headers.get('x-csrf-token') });
      return make();
    })
  );

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  searches = [];
  posted = [];
  document.cookie = 'csrf=tok123';
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

async function search(text: string) {
  const user = userEvent.setup();
  await user.type(screen.getByRole('searchbox', { name: 'Votre recherche' }), text);
  await user.click(screen.getByRole('button', { name: 'Chercher' }));
  return user;
}

describe('Search screen, administrator', () => {
  // req: EF-UX-006
  it('is a labelled search form under a first-level heading, and asks nothing before a query', async () => {
    answerSearch(() => HttpResponse.json(federated()));
    render(Page);
    expect(screen.getByRole('heading', { level: 1, name: 'Chercher' })).toBeInTheDocument();
    expect(screen.getByRole('search')).toBeInTheDocument();
    expect(screen.getByRole('searchbox', { name: 'Votre recherche' })).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Chercher' })); // blank: nothing goes out
    await user.type(screen.getByRole('searchbox', { name: 'Votre recherche' }), '   {Enter}');
    expect(searches).toEqual([]);
  });

  // req: EF-UX-011
  it('test_search_screen_lists_searched_in_and_request', async () => {
    answerSearch(() => HttpResponse.json(empty()));
    answerRequests(() => HttpResponse.json(requested({ title: 'zzz introuvable' }), { status: 201 }));
    render(Page);
    const user = await search('zzz introuvable');

    // the question: the words as typed, the whole federation
    await waitFor(() => expect(searches).toHaveLength(1));
    expect(searches[0]!.searchParams.get('q')).toBe('zzz introuvable');
    expect(searches[0]!.searchParams.get('scope')).toBe('all');

    // 0 result: honest, with where it looked (and who did not answer), and the way out
    const where = await screen.findByRole('region', { name: 'Recherché dans' });
    const rows = within(where).getAllByTestId('searched-source');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('Alpha texts');
    expect(rows[0]).toHaveTextContent('aucun résultat');
    expect(rows[1]).toHaveTextContent('Beta scans');
    expect(rows[1]).toHaveTextContent('Indisponible');
    expect(screen.getByText('Aucun résultat pour cette recherche.')).toBeInTheDocument();
    expect(screen.queryByTestId('search-result')).toBeNull();
    expect(screen.queryByRole('button', { name: /^Ajouter/ })).toBeNull();

    // « Demander »: the words typed, as free text, with the CSRF token, once
    await user.click(screen.getByRole('button', { name: 'Demander' }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toEqual({
      body: { target_kind: 'free_text', target_value: 'zzz introuvable' },
      csrf: 'tok123'
    });
    expect(await screen.findByRole('status')).toHaveTextContent('Demande envoyée');
    expect(screen.queryByRole('button', { name: 'Demander' })).toBeNull(); // not twice
    expect(screen.queryByRole('link', { name: 'Ouvrir dans Kavita' })).toBeNull();
  });

  // req: EF-UX-006
  it('offers « Ouvrir dans Kavita » when what was requested is already there', async () => {
    answerSearch(() => HttpResponse.json(empty()));
    answerRequests(() =>
      HttpResponse.json(requested({ state: 'available', family_state: 'available', open_url: KAVITA }), { status: 201 })
    );
    render(Page);
    const user = await search('déjà là');
    await user.click(await screen.findByRole('button', { name: 'Demander' }));
    const link = await screen.findByRole('link', { name: 'Ouvrir dans Kavita' });
    expect(link).toHaveAttribute('href', KAVITA);
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'));
  });

  // req: EF-UX-006
  it('refuses a link that is not a web address, and shows no link', async () => {
    answerSearch(() => HttpResponse.json(empty()));
    answerRequests(() => HttpResponse.json(requested({ open_url: 'javascript:alert(1)' }), { status: 201 }));
    render(Page);
    const user = await search('x');
    await user.click(await screen.findByRole('button', { name: 'Demander' }));
    await screen.findByRole('status');
    expect(screen.queryByRole('link', { name: 'Ouvrir dans Kavita' })).toBeNull();
  });

  // req: EF-UX-006
  it('lists the Works found, each Edition with its source and format, and an add verb per Edition', async () => {
    answerSearch(() => HttpResponse.json(federated()));
    render(Page);
    await search('hobbit');
    const results = await screen.findAllByTestId('search-result');
    expect(results).toHaveLength(2);
    expect(within(results[0]!).getByRole('heading', { level: 2, name: 'Le Hobbit' })).toBeInTheDocument();
    const editions = within(results[0]!).getAllByRole('listitem');
    expect(editions).toHaveLength(2);
    expect(editions[0]).toHaveTextContent('Alpha texts');
    expect(editions[0]).toHaveTextContent('Texte');
    expect(editions[1]).toHaveTextContent('Beta scans');
    expect(editions[1]).toHaveTextContent('Images');
    for (const row of editions) {
      expect(within(row).getByRole('button', { name: /^Ajouter à ma bibliothèque/ })).toBeEnabled();
    }
    // the same verb on two Editions must not read the same to a screen reader
    const names = within(results[0]!)
      .getAllByRole('button', { name: /^Ajouter à ma bibliothèque/ })
      .map((b) => b.getAttribute('aria-label') ?? b.textContent);
    expect(new Set(names).size).toBe(2);
    // results are not requests: « Demander » is for what was not found
    expect(screen.queryByRole('button', { name: 'Demander' })).toBeNull();
    // and where it looked comes with them
    expect(screen.getByRole('region', { name: 'Recherché dans' })).toBeInTheDocument();
    expect(screen.getAllByRole('list', { name: 'Niveau de confiance' })).toHaveLength(1); // alpha only
  });

  // req: EF-UX-006, EF-BIB-030
  it('opens the sheet of the Edition chosen, with its source, and gives the focus back on close', async () => {
    answerSearch(() => HttpResponse.json(federated()));
    server.use(
      http.get(`${API}/shelves`, () => HttpResponse.json({ items: [{ id: 'S1', name: 'Famille', max_age_rating: 13 }], since: 'x', removed: [] })),
      http.get(`${API}/sources/:key/series/:rid/estimate`, ({ params }) =>
        HttpResponse.json({
          mode: 'all',
          units: params.key === 'beta' ? 12 : 3,
          size_bytes: 1000,
          free_bytes: 10 ** 12,
          needs_confirmation: false
        })
      )
    );
    render(Page);
    const user = await search('hobbit');
    const results = await screen.findAllByTestId('search-result');
    const beta = within(within(results[0]!).getAllByRole('listitem')[1]!).getByRole('button', {
      name: /^Ajouter à ma bibliothèque/
    });
    await user.click(beta);
    const dialog = await screen.findByRole('dialog', { name: /^Ajouter à ma bibliothèque/ });
    expect(within(dialog).getByText('Der Hobbit')).toBeInTheDocument();
    expect(within(dialog).getByText('Beta scans')).toBeInTheDocument();
    await waitFor(() => expect(within(dialog).getByTestId('add-summary')).toHaveTextContent(/12\sunités/));
    await user.click(within(dialog).getByRole('button', { name: 'Fermer' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(beta).toHaveFocus();
  });

  // req: EF-UX-006
  it('once an Edition is added, it is not offered again', async () => {
    answerSearch(() => HttpResponse.json(federated()));
    server.use(
      http.get(`${API}/shelves`, () => HttpResponse.json({ items: [], since: 'x', removed: [] })),
      http.get(`${API}/sources/:key/series/:rid/estimate`, () =>
        HttpResponse.json({ mode: 'all', units: 3, size_bytes: 1000, free_bytes: 10 ** 12, needs_confirmation: false })
      ),
      http.post(`${API}/library/space-check`, () =>
        HttpResponse.json({ fits: true, estimated_bytes: 1000, free_bytes: 10 ** 12, reserve_bytes: 0 })
      ),
      http.post(`${API}/follows`, () => HttpResponse.json({ state: 'followed', sent: true }))
    );
    render(Page);
    const user = await search('hobbit');
    const first = (await screen.findAllByTestId('search-result'))[0]!;
    const row = within(first).getAllByRole('listitem')[0]!;
    await user.click(within(row).getByRole('button', { name: /^Ajouter à ma bibliothèque/ }));
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByTestId('add-summary')).toBeInTheDocument());
    await user.click(within(dialog).getByRole('button', { name: 'Ajouter à ma bibliothèque' }));
    await within(dialog).findByText('Ajouté à votre bibliothèque');
    await user.click(within(dialog).getByRole('button', { name: 'Fermer' }));
    expect(within(row).queryByRole('button', { name: /^Ajouter/ })).toBeNull();
    expect(row).toHaveTextContent('Dans votre bibliothèque');
    // the other Edition is still there to add
    expect(within(within(first).getAllByRole('listitem')[1]!).getByRole('button', { name: /^Ajouter/ })).toBeEnabled();
  });

  // req: EF-UX-011
  it('continues with the cursor the server gave, appends, and stops when there is no more', async () => {
    answerSearch((url) =>
      url.searchParams.get('cursor') === 'C1'
        ? HttpResponse.json(federated({ items: [group('G3', 'Troisième', [edition('alpha', 'a3', 'Troisième', 'text')])], next_cursor: null }))
        : HttpResponse.json(federated({ next_cursor: 'C1' }))
    );
    render(Page);
    const user = await search('hobbit');
    await screen.findAllByTestId('search-result');
    await user.click(screen.getByRole('button', { name: 'Plus de résultats' }));
    await waitFor(() => expect(screen.getAllByTestId('search-result')).toHaveLength(3));
    expect(searches.at(-1)!.searchParams.get('cursor')).toBe('C1');
    expect(searches.at(-1)!.searchParams.get('q')).toBe('hobbit');
    expect(searches.at(-1)!.searchParams.get('scope')).toBe('all');
    expect(screen.getByRole('heading', { level: 2, name: 'Le Hobbit' })).toBeInTheDocument(); // kept
    expect(screen.queryByRole('button', { name: 'Plus de résultats' })).toBeNull();
  });

  // req: EF-UX-011
  it('a new query starts again: no old result, no old cursor', async () => {
    answerSearch((url) =>
      HttpResponse.json(
        url.searchParams.get('q') === 'hobbit'
          ? federated({ next_cursor: 'C1' })
          : federated({ items: [group('G9', 'Dune', [edition('alpha', 'd1', 'Dune', 'text')])] })
      )
    );
    render(Page);
    const user = await search('hobbit');
    await screen.findAllByTestId('search-result');
    const box = screen.getByRole('searchbox', { name: 'Votre recherche' });
    await user.clear(box);
    await user.type(box, 'dune');
    await user.click(screen.getByRole('button', { name: 'Chercher' }));
    await waitFor(() => expect(screen.getAllByTestId('search-result')).toHaveLength(1));
    expect(screen.getByRole('heading', { level: 2, name: 'Dune' })).toBeInTheDocument();
    expect(screen.queryByText('Le Hobbit')).toBeNull();
    expect(searches.at(-1)!.searchParams.has('cursor')).toBe(false);
    expect(screen.queryByRole('button', { name: 'Plus de résultats' })).toBeNull();
  });

  // req: EF-UX-011
  it('when every source failed, says so and lists them, and offers no request that would hide it', async () => {
    answerSearch(() =>
      HttpResponse.json(
        federated({
          items: [],
          state: 'unavailable',
          action: null,
          searched_in: asked({ status: 'error', code: 'source.timeout', returned: 0, trust: null })
        })
      )
    );
    render(Page);
    await search('hobbit');
    expect(await screen.findByText('Les sources ne répondent pas pour le moment.')).toBeInTheDocument();
    expect(screen.getAllByTestId('searched-source')).toHaveLength(2);
    expect(screen.queryByRole('button', { name: 'Demander' })).toBeNull();
    expect(screen.queryByText('Aucun résultat pour cette recherche.')).toBeNull();
  });

  // req: EF-UX-011, EF-UX-008
  it('with no source switched on, says so and leads to the settings, with no list of sites', async () => {
    answerSearch(() => HttpResponse.json(federated({ items: [], state: 'no_sources', action: null, searched_in: [], formats: [] })));
    render(Page);
    await search('hobbit');
    expect(await screen.findByText('Aucune source n’est activée.')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Ouvrir les réglages' })).toHaveAttribute('href', '/reglages');
    expect(screen.queryByRole('region', { name: 'Recherché dans' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Demander' })).toBeNull();
  });

  // req: EF-UX-003
  it('says it is searching while the sources answer, and the form stays usable', async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    answerSearch(async () => {
      await gate;
      return HttpResponse.json(federated());
    });
    render(Page);
    await search('hobbit');
    expect(await screen.findByRole('status')).toHaveTextContent('Recherche en cours');
    expect(screen.getByTestId('search-results')).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('searchbox', { name: 'Votre recherche' })).toBeEnabled();
    release();
    await screen.findAllByTestId('search-result');
    expect(screen.queryByText('Recherche en cours')).toBeNull();
    expect(screen.getByTestId('search-results')).not.toHaveAttribute('aria-busy', 'true');
  });

  // req: EF-UX-003
  it('shows the problem of a search that failed, with no result, and lets the person search again', async () => {
    let calls = 0;
    answerSearch(() => {
      calls += 1;
      return calls === 1 ? problem(500, 'internal') : HttpResponse.json(federated());
    });
    render(Page);
    const user = await search('hobbit');
    expect(await screen.findByRole('alert')).toHaveTextContent('Titre internal');
    expect(screen.queryByTestId('search-result')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Chercher' }));
    await screen.findAllByTestId('search-result');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  // req: EF-UX-003
  it('shows the problem of a request that failed, and keeps the button to try again', async () => {
    answerSearch(() => HttpResponse.json(empty()));
    answerRequests(() => problem(409, 'requests.duplicate'));
    render(Page);
    const user = await search('zzz');
    await user.click(await screen.findByRole('button', { name: 'Demander' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Titre requests.duplicate');
    expect(screen.getByRole('button', { name: 'Demander' })).toBeEnabled();
    expect(screen.queryByText('Demande envoyée')).toBeNull();
  });

  // req: EF-USR-014
  it('a member, refused the federation, gets the catalogue and nothing to add or request', async () => {
    answerSearch((url) =>
      url.searchParams.get('scope') === 'all'
        ? problem(403, 'forbidden')
        : HttpResponse.json({ items: [{ work_id: 'W1', title: 'Dans la bibliothèque', kind: 'book' }], total: 1 })
    );
    render(Page);
    await search('biblio');
    expect(await screen.findByText('Dans la bibliothèque')).toBeInTheDocument();
    expect(searches.map((u) => u.searchParams.get('scope'))).toEqual(['all', 'local']);
    expect(screen.queryByRole('button', { name: /^Ajouter|Demander/ })).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });
});

describe('Search screen, child', () => {
  // req: EF-USR-014
  it('test_child_search_screen_shows_title_only', async () => {
    answerSearch(() => HttpResponse.json(family()));
    answerRequests(() => HttpResponse.json(requested({ title: 'Conte tranquille' }), { status: 201 }));
    const { container } = render(Page);
    const user = await search('conte');

    const items = await screen.findAllByTestId('family-result');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('Conte tranquille');
    expect(items[1]).toHaveTextContent('Autre conte');
    // a title and the one verb: nothing to open, nothing to add, nothing to count
    for (const item of items) {
      expect(within(item).getAllByRole('button').map((b) => b.textContent?.trim())).toEqual(['Demander']);
      expect(within(item).queryByRole('link')).toBeNull();
      expect(item.querySelector('img, svg, picture')).toBeNull();
    }
    expect(container.querySelector('img, picture')).toBeNull();
    const text = document.body.textContent ?? '';
    expect(text).not.toMatch(/Ajouter|Exporter|Ouvrir|Recherché dans|Alpha|Beta|source|Officiel|Communauté|Niveau|REF1|REF2|\bformat\b|\d+\s+résultats?/i);
    expect(screen.queryByRole('region', { name: 'Recherché dans' })).toBeNull();
    expect(screen.queryByTestId('searched-source')).toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();

    // what she may already read is listed as a title, with no verb at all
    const local = screen.getAllByTestId('local-result');
    expect(local).toHaveLength(1);
    expect(local[0]).toHaveTextContent('Déjà sur l’étagère');
    expect(within(local[0]!).queryByRole('button')).toBeNull();
    expect(within(local[0]!).queryByRole('link')).toBeNull();

    // « Demander » sends the opaque reference, and only that
    await user.click(within(items[0]!).getByRole('button', { name: 'Demander' }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toEqual({ body: { request_ref: 'REF1' }, csrf: 'tok123' });
    expect(await within(items[0]!).findByRole('status')).toHaveTextContent('Demande envoyée');
    expect(within(items[0]!).queryByRole('button', { name: 'Demander' })).toBeNull();
    expect(within(items[1]!).getByRole('button', { name: 'Demander' })).toBeEnabled();
    expect(searches[0]!.searchParams.get('scope')).toBe('all');
  });

  // req: EF-USR-014
  it('asks for the second Work with its own reference', async () => {
    answerSearch(() => HttpResponse.json(family({ items: [{ title: 'Seul', action: 'request', request_ref: 'ZZ-42' }], local: [] })));
    answerRequests();
    render(Page);
    const user = await search('seul');
    await user.click(await screen.findByRole('button', { name: 'Demander' }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]!.body).toEqual({ request_ref: 'ZZ-42' });
    expect(screen.queryByTestId('local-result')).toBeNull();
  });

  // req: EF-USR-014
  it('shows a title as text, never as markup', async () => {
    answerSearch(() =>
      HttpResponse.json(family({ items: [{ title: '<b>Gras</b><img src=x>', action: 'request', request_ref: 'R' }], local: [] }))
    );
    const { container } = render(Page);
    await search('x');
    await screen.findByTestId('family-result');
    expect(container.querySelector('b, img')).toBeNull();
    expect(screen.getByTestId('family-result')).toHaveTextContent('<b>Gras</b><img src=x>');
  });

  // req: EF-UX-011, EF-USR-014
  it('with nothing found, says so without a list of sources or a count, and lets her ask', async () => {
    answerSearch(() => HttpResponse.json(family({ items: [], local: [], state: 'empty' })));
    answerRequests();
    render(Page);
    const user = await search('introuvable');
    expect(await screen.findByText('Aucun résultat pour cette recherche.')).toBeInTheDocument();
    expect(screen.queryByTestId('searched-source')).toBeNull();
    expect(screen.queryByRole('region', { name: 'Recherché dans' })).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Demander' }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]!.body).toEqual({ target_kind: 'free_text', target_value: 'introuvable' });
  });

  // req: EF-USR-014
  it('continues with the cursor, and the second page is as reduced as the first', async () => {
    answerSearch((url) =>
      url.searchParams.get('cursor') === 'F1'
        ? HttpResponse.json(family({ items: [{ title: 'Suite', action: 'request', request_ref: 'REF3' }], local: [], next_cursor: null }))
        : HttpResponse.json(family({ next_cursor: 'F1' }))
    );
    render(Page);
    const user = await search('conte');
    await screen.findAllByTestId('family-result');
    await user.click(screen.getByRole('button', { name: 'Plus de résultats' }));
    await waitFor(() => expect(screen.getAllByTestId('family-result')).toHaveLength(3));
    expect(searches.at(-1)!.searchParams.get('cursor')).toBe('F1');
    expect(document.body.textContent).not.toMatch(/Ajouter|Exporter|Recherché dans/);
  });

  // req: EF-UX-003
  it('shows the problem of a request that failed, and keeps the button', async () => {
    answerSearch(() => HttpResponse.json(family()));
    answerRequests(() => problem(429, 'rate_limited'));
    render(Page);
    const user = await search('conte');
    const [first] = await screen.findAllByTestId('family-result');
    await user.click(within(first!).getByRole('button', { name: 'Demander' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Titre rate_limited');
    expect(within(first!).getByRole('button', { name: 'Demander' })).toBeEnabled();
  });
});

describe('Search screen, English', () => {
  // req: ENF-I18N-001
  it('speaks English, in the same places, with nothing left in French', async () => {
    answerSearch(() => HttpResponse.json(empty()));
    try {
      setUiLanguage('en');
      render(Page);
      const user = userEvent.setup();
      await user.type(screen.getByRole('searchbox', { name: 'Your search' }), 'zzz');
      await user.click(screen.getByRole('button', { name: 'Search' }));
      expect(await screen.findByRole('region', { name: 'Searched in' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Request' })).toBeInTheDocument();
      expect(screen.getByText('No result for this search.')).toBeInTheDocument();
      const rows = screen.getAllByTestId('searched-source');
      expect(rows[0]).toHaveTextContent('no results');
      expect(rows[1]).toHaveTextContent('Unavailable');
      expect(document.body.textContent).not.toMatch(/Recherch|Demander|Aucun|Indisponible|résultat/);
    } finally {
      setUiLanguage('fr');
    }
  });
});
