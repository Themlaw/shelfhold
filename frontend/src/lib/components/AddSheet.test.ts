import { render, screen, waitFor, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import AddSheet from './AddSheet.svelte';

// The « Ajouter à ma bibliothèque » sheet (EF-UX-006, EF-BIB-030): the count, the size and the
// shelf of what would be added, the three modes, and the refusal that offers « Vider la
// corbeille ». The sheet decides nothing: the numbers are the server's, the refusal is the
// server's, and nothing is written before the one click.
const BASE = 'http://shelfhold.test';
const API = `${BASE}/api/v1`;
const GIB = 1024 ** 3;
const server = setupServer();

const EDITION = { source_key: 'alpha', remote_id: 'a1', title: 'Un livre cherché', format: 'text' };
const SHELVES = [
  { id: 'SHELF_FAMILLE', name: 'Famille', max_age_rating: 13 },
  { id: 'SHELF_ADULTES', name: 'Adultes', max_age_rating: null }
];
const FIGURES: Record<string, { units: number; size_bytes: number }> = {
  all: { units: 1000, size_bytes: 62_914_560 },
  new_only: { units: 4, size_bytes: 1_048_576 },
  '12': { units: 38, size_bytes: 7_340_032 }
};

const problem = (status: number, code: string, extra: Record<string, unknown> = {}) =>
  HttpResponse.json(
    { type: `/api/v1/problems/${code}`, title: `Titre ${code}`, status, code, ...extra },
    { status }
  );

type Seen = { url: URL; csrf: string | null; body?: unknown };
let estimates: Seen[] = [];
let spaceChecks: Seen[] = [];
let follows: Seen[] = [];
let trashes: Seen[] = [];

/** The estimate the server gives for a mode (and a number), with the flag the test wants. */
function estimateFor(url: URL, confirm = false) {
  const mode = url.searchParams.get('mode') ?? 'all';
  const figures = FIGURES[mode === 'from_number' ? (url.searchParams.get('from_number') ?? '') : mode];
  return {
    mode,
    units: figures?.units ?? 0,
    size_bytes: figures?.size_bytes ?? 0,
    free_bytes: 500 * GIB,
    needs_confirmation: confirm
  };
}

function serve(
  over: {
    estimate?: (url: URL) => Response | Promise<Response>;
    spaceCheck?: (n: number) => Response;
    follow?: () => Response;
    shelves?: () => Response;
    trash?: (n: number) => Response | Promise<Response>;
  } = {}
) {
  server.use(
    http.get(`${API}/shelves`, () =>
      over.shelves ? over.shelves() : HttpResponse.json({ items: SHELVES, since: 'x', removed: [] })
    ),
    http.get(`${API}/sources/:key/series/:rid/estimate`, ({ request, params }) => {
      const url = new URL(request.url);
      estimates.push({ url: Object.assign(url, { key: params.key, rid: params.rid }) as URL, csrf: null });
      return over.estimate ? over.estimate(url) : HttpResponse.json(estimateFor(url));
    }),
    http.post(`${API}/library/space-check`, async ({ request }) => {
      spaceChecks.push({
        url: new URL(request.url),
        csrf: request.headers.get('x-csrf-token'),
        body: await request.json()
      });
      return over.spaceCheck
        ? over.spaceCheck(spaceChecks.length)
        : HttpResponse.json({ fits: true, estimated_bytes: 1, free_bytes: 2, reserve_bytes: 0 });
    }),
    http.post(`${API}/follows`, async ({ request }) => {
      follows.push({
        url: new URL(request.url),
        csrf: request.headers.get('x-csrf-token'),
        body: await request.json()
      });
      return over.follow ? over.follow() : HttpResponse.json({ state: 'followed', sent: true });
    }),
    http.post(`${API}/trash/empty`, async ({ request }) => {
      trashes.push({
        url: new URL(request.url),
        csrf: request.headers.get('x-csrf-token'),
        body: await request.json()
      });
      return over.trash
        ? over.trash(trashes.length)
        : HttpResponse.json({ removed: 12, kept: 0, held_irreplaceable: 0, held_other: 0, failed: 0 });
    })
  );
}

const refusal = (estimated: number, free: number, reserve: number) =>
  problem(409, 'library.space_insufficient', {
    estimated_bytes: estimated,
    free_bytes: free,
    reserve_bytes: reserve,
    reclaim_action: 'empty_trash'
  });

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  estimates = [];
  spaceChecks = [];
  follows = [];
  trashes = [];
  document.cookie = 'csrf=tok123';
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function open(props: Record<string, unknown> = {}) {
  const onclose = vi.fn();
  const onadded = vi.fn();
  const view = render(AddSheet, {
    edition: EDITION,
    sourceName: 'Alpha texts',
    onclose,
    onadded,
    ...props
  } as never);
  return { onclose, onadded, ...view };
}
const sheet = () => screen.findByRole('dialog', { name: /^Ajouter à ma bibliothèque/ });
const addButton = () => screen.getByRole('button', { name: 'Ajouter à ma bibliothèque' });
const summary = () => screen.getByTestId('add-summary');

describe('AddSheet', () => {
  // req: EF-UX-006
  it('test_add_sheet_shows_count_size_shelf_and_modes', async () => {
    serve();
    open();
    const dialog = await sheet();
    expect(within(dialog).getByText('Un livre cherché')).toBeInTheDocument();
    expect(within(dialog).getByText('Alpha texts')).toBeInTheDocument();

    // the count and the size of the default mode, as the server counted them
    await waitFor(() => expect(summary()).toHaveTextContent(/1\s000\sunités,\s62\s914\s560\soctets/));
    expect(estimates).toHaveLength(1);
    expect(estimates[0]!.url.searchParams.get('mode')).toBe('all');
    expect(estimates[0]!.url.searchParams.has('from_number')).toBe(false);
    expect(estimates[0]!.url).toMatchObject({ key: 'alpha', rid: 'a1' });

    // the shelf: the choices of the server, the first one chosen
    const shelf = within(dialog).getByRole('combobox', { name: 'Étagère' });
    expect(within(shelf).getAllByRole('option').map((o) => o.textContent)).toEqual(['Famille', 'Adultes']);
    expect(shelf).toHaveValue('SHELF_FAMILLE');

    // the three modes, one chosen at a time, "Tout" first
    const modes = within(dialog).getByRole('group', { name: 'Quoi ajouter' });
    const radios = within(modes).getAllByRole('radio');
    expect(radios.map((r) => (r as HTMLInputElement).labels?.[0]?.textContent?.trim())).toEqual([
      'Tout',
      'À partir du numéro',
      'Nouveaux seulement'
    ]);
    expect(within(modes).getByRole('radio', { name: 'Tout' })).toBeChecked();
    expect(within(modes).getByRole('radio', { name: 'Nouveaux seulement' })).not.toBeChecked();

    // nothing was written, nothing was checked, just by looking
    expect(spaceChecks).toEqual([]);
    expect(follows).toEqual([]);
  });

  // req: EF-UX-006
  it('asks the estimate again for each mode, and shows the figures of that mode', async () => {
    serve();
    open();
    const user = userEvent.setup();
    await waitFor(() => expect(summary()).toHaveTextContent(/1\s000\sunités/));

    await user.click(screen.getByRole('radio', { name: 'Nouveaux seulement' }));
    await waitFor(() => expect(summary()).toHaveTextContent(/4\sunités,\s1\s048\s576\soctets/));
    expect(estimates.at(-1)!.url.searchParams.get('mode')).toBe('new_only');
    expect(estimates.at(-1)!.url.searchParams.has('from_number')).toBe(false);

    // the number is asked only for its own mode, and the sheet waits for it
    await user.click(screen.getByRole('radio', { name: 'À partir du numéro' }));
    const number = await screen.findByRole('textbox', { name: 'Numéro de départ' });
    expect(addButton()).toBeDisabled();
    const before = estimates.length;
    await user.type(number, '12');
    await waitFor(() => expect(summary()).toHaveTextContent(/38\sunités,\s7\s340\s032\soctets/));
    expect(estimates.at(-1)!.url.searchParams.get('mode')).toBe('from_number');
    expect(estimates.at(-1)!.url.searchParams.get('from_number')).toBe('12');
    expect(estimates.length).toBeGreaterThan(before);
    expect(addButton()).toBeEnabled();

    // back to everything: the number box goes, and the request carries no number any more
    await user.click(screen.getByRole('radio', { name: 'Tout' }));
    await waitFor(() => expect(summary()).toHaveTextContent(/1\s000\sunités/));
    expect(screen.queryByRole('textbox', { name: 'Numéro de départ' })).toBeNull();
    expect(estimates.at(-1)!.url.searchParams.has('from_number')).toBe(false);
  });

  // req: EF-UX-006
  it('shows the figures of the latest choice, never those of a slower, older answer', async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    serve({
      estimate: async (url) => {
        if (url.searchParams.get('from_number') === '2') {
          await gate;
          return HttpResponse.json({ ...estimateFor(url), units: 99, size_bytes: 99_000_000 });
        }
        if (url.searchParams.get('from_number') === '25') {
          return HttpResponse.json({ ...estimateFor(url), units: 7, size_bytes: 7_000_000 });
        }
        return HttpResponse.json(estimateFor(url));
      }
    });
    open();
    const user = userEvent.setup();
    await waitFor(() => expect(summary()).toHaveTextContent(/1\s000\sunités/));
    await user.click(screen.getByRole('radio', { name: 'À partir du numéro' }));
    await user.type(await screen.findByRole('textbox', { name: 'Numéro de départ' }), '25');
    await waitFor(() => expect(summary()).toHaveTextContent(/7\sunités,\s7\s000\s000\soctets/));
    release();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(summary()).toHaveTextContent(/7\sunités,\s7\s000\s000\soctets/);
    expect(summary()).not.toHaveTextContent('99');
  });

  // req: EF-UX-006
  it('adds with the chosen mode, number and shelf: space checked first, then one follow', async () => {
    serve();
    const { onadded } = open();
    const user = userEvent.setup();
    await waitFor(() => expect(summary()).toHaveTextContent(/1\s000\sunités/));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Étagère' }), 'SHELF_ADULTES');
    await user.click(screen.getByRole('radio', { name: 'À partir du numéro' }));
    await user.type(await screen.findByRole('textbox', { name: 'Numéro de départ' }), '12');
    await waitFor(() => expect(summary()).toHaveTextContent(/38\sunités/));
    await user.click(addButton());

    await waitFor(() => expect(onadded).toHaveBeenCalledOnce());
    expect(onadded).toHaveBeenCalledWith('followed');
    expect(spaceChecks).toHaveLength(1);
    expect(spaceChecks[0]!.body).toEqual({ estimated_bytes: 7_340_032 });
    expect(spaceChecks[0]!.csrf).toBe('tok123');
    expect(follows).toHaveLength(1);
    expect(follows[0]!.body).toEqual({
      source_key: 'alpha',
      remote_id: 'a1',
      mode: 'from_number',
      from_number: '12',
      shelf_id: 'SHELF_ADULTES'
    });
    expect(follows[0]!.csrf).toBe('tok123');
    expect(screen.getByRole('status')).toHaveTextContent('Ajouté à votre bibliothèque');
  });

  // req: EF-UX-006
  it.each([
    ['all', { source_key: 'alpha', remote_id: 'a1', mode: 'all', shelf_id: 'SHELF_FAMILLE' }],
    ['new_only', { source_key: 'alpha', remote_id: 'a1', mode: 'new_only', shelf_id: 'SHELF_FAMILLE' }]
  ])('sends no number with the mode %s', async (mode, expected) => {
    serve();
    open();
    const user = userEvent.setup();
    await waitFor(() => expect(summary()).toHaveTextContent(/unités/));
    if (mode === 'new_only') await user.click(screen.getByRole('radio', { name: 'Nouveaux seulement' }));
    await waitFor(() => expect(summary()).toHaveTextContent(mode === 'all' ? /1\s000/ : /4\sunités/));
    await user.click(addButton());
    await waitFor(() => expect(follows).toHaveLength(1));
    expect(follows[0]!.body).toEqual(expected);
  });

  // req: EF-UX-006
  it('uses the edition it was given, not a fixed one', async () => {
    serve();
    open({ edition: { source_key: 'beta', remote_id: 'b/7', title: 'Autre', format: 'images' } });
    const user = userEvent.setup();
    await waitFor(() => expect(summary()).toHaveTextContent(/unités/));
    expect(estimates[0]!.url).toMatchObject({ key: 'beta', rid: 'b/7' });
    await user.click(addButton());
    await waitFor(() => expect(follows).toHaveLength(1));
    expect(follows[0]!.body).toMatchObject({ source_key: 'beta', remote_id: 'b/7' });
  });

  // req: EF-UX-006
  it('with no shelf offered, names where it will go and sends no shelf', async () => {
    serve({ shelves: () => HttpResponse.json({ items: [], since: 'x', removed: [] }) });
    open();
    await waitFor(() => expect(summary()).toHaveTextContent(/unités/));
    expect(screen.queryByRole('combobox', { name: 'Étagère' })).toBeNull();
    expect(screen.getByText(/À classer/)).toBeInTheDocument();
    await userEvent.setup().click(addButton());
    await waitFor(() => expect(follows).toHaveLength(1));
    expect(follows[0]!.body).not.toHaveProperty('shelf_id');
  });

  // req: EF-UX-006, EF-EXP-053
  it('over the threshold, the size must be confirmed first, and the figures seen travel with the add', async () => {
    serve({
      estimate: (url) =>
        HttpResponse.json({ ...estimateFor(url), units: 120, size_bytes: 6 * GIB, needs_confirmation: true })
    });
    open();
    const user = userEvent.setup();
    await waitFor(() => expect(summary()).toHaveTextContent(/120\sunités/));
    expect(addButton()).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: 'Je confirme la taille' }));
    expect(addButton()).toBeEnabled();
    await user.click(addButton());
    await waitFor(() => expect(follows).toHaveLength(1));
    expect(follows[0]!.body).toMatchObject({ confirm_units: 120, confirm_bytes: 6 * GIB });
  });

  // req: EF-UX-006
  it('below the threshold asks for no confirmation and sends none', async () => {
    serve();
    open();
    await waitFor(() => expect(summary()).toHaveTextContent(/unités/));
    expect(screen.queryByRole('checkbox')).toBeNull();
    await userEvent.setup().click(addButton());
    await waitFor(() => expect(follows).toHaveLength(1));
    expect(follows[0]!.body).not.toHaveProperty('confirm_units');
    expect(follows[0]!.body).not.toHaveProperty('confirm_bytes');
  });

  // req: EF-UX-006
  it('tells a follow that is already on its way, and a follow that failed', async () => {
    serve({ follow: () => HttpResponse.json({ state: 'in_progress', sent: false }) });
    const { onadded } = open();
    await waitFor(() => expect(summary()).toHaveTextContent(/unités/));
    await userEvent.setup().click(addButton());
    await waitFor(() => expect(onadded).toHaveBeenCalledWith('in_progress'));
    expect(screen.getByRole('status')).toHaveTextContent('Ajout en cours');
  });

  // req: EF-UX-006
  it('shows the problem of a refused follow, keeps the sheet and lets the person try again', async () => {
    serve({ follow: () => problem(409, 'follow.service_paused') });
    const { onadded } = open();
    const user = userEvent.setup();
    await waitFor(() => expect(summary()).toHaveTextContent(/unités/));
    await user.click(addButton());
    expect(await screen.findByRole('alert')).toHaveTextContent('Titre follow.service_paused');
    expect(onadded).not.toHaveBeenCalled();
    expect(addButton()).toBeEnabled();
  });

  // req: EF-BIB-030
  it('test_add_sheet_free_space_refusal_offers_empty_trash', async () => {
    serve({ spaceCheck: () => refusal(60 * GIB, 40 * GIB, 10 * GIB) });
    const { onadded } = open();
    const user = userEvent.setup();
    await waitFor(() => expect(summary()).toHaveTextContent(/unités/));
    await user.click(addButton());

    // the refusal says the three figures the server gave, and offers the action
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/64\s424\s509\s440/); // 60 GiB, what is asked
    expect(alert).toHaveTextContent(/42\s949\s672\s960/); // 40 GiB, what is free
    expect(alert).toHaveTextContent(/10\s737\s418\s240/); // 10 GiB, the reserve kept
    expect(within(alert).getByRole('button', { name: 'Vider la corbeille' })).toBeEnabled();
    // nothing was added, nothing was emptied by itself
    expect(follows).toEqual([]);
    expect(trashes).toEqual([]);
    expect(onadded).not.toHaveBeenCalled();
  });

  // req: EF-BIB-030
  it('empties the trash only on the typed word, then checks the space again and lets the add go', async () => {
    serve({
      spaceCheck: (n) =>
        n === 1
          ? refusal(60 * GIB, 40 * GIB, 10 * GIB)
          : HttpResponse.json({ fits: true, estimated_bytes: 60 * GIB, free_bytes: 90 * GIB, reserve_bytes: 10 * GIB })
    });
    const { onadded } = open();
    const user = userEvent.setup();
    await waitFor(() => expect(summary()).toHaveTextContent(/unités/));
    await user.click(addButton());
    await user.click(await screen.findByRole('button', { name: 'Vider la corbeille' }));

    // a reserved action: the word is typed, and anything else sends nothing
    const word = screen.getByRole('textbox', { name: /empty-trash/ });
    const confirm = screen.getByRole('button', { name: 'Vider maintenant' });
    expect(confirm).toBeDisabled();
    await user.type(word, 'empty-tras');
    expect(confirm).toBeDisabled();
    await user.click(confirm);
    expect(trashes).toEqual([]);
    await user.type(word, 'h');
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() => expect(trashes).toHaveLength(1));
    expect(trashes[0]!.body).toEqual({ typed: 'empty-trash' });
    expect(trashes[0]!.csrf).toBe('tok123');
    // the space is checked again, with the same estimate, and the refusal goes away
    await waitFor(() => expect(spaceChecks).toHaveLength(2));
    expect(spaceChecks[1]!.body).toEqual(spaceChecks[0]!.body);
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    expect(screen.getByRole('status')).toHaveTextContent('Corbeille vidée');
    expect(follows).toEqual([]); // emptying is not adding: one more click does that
    expect(onadded).not.toHaveBeenCalled();
    await user.click(addButton());
    await waitFor(() => expect(onadded).toHaveBeenCalledWith('followed'));
    expect(follows).toHaveLength(1);
  });

  // req: EF-BIB-030
  it('when the space is still short after emptying, says so with the new figures and adds nothing', async () => {
    serve({
      spaceCheck: (n) =>
        n === 1 ? refusal(60 * GIB, 40 * GIB, 10 * GIB) : refusal(60 * GIB, 45 * GIB, 10 * GIB)
    });
    open();
    const user = userEvent.setup();
    await waitFor(() => expect(summary()).toHaveTextContent(/unités/));
    await user.click(addButton());
    await user.click(await screen.findByRole('button', { name: 'Vider la corbeille' }));
    await user.type(screen.getByRole('textbox', { name: /empty-trash/ }), 'empty-trash');
    await user.click(screen.getByRole('button', { name: 'Vider maintenant' }));
    await waitFor(() => expect(spaceChecks).toHaveLength(2));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/48\s318\s382\s080/)); // 45 GiB
    expect(screen.getByRole('alert')).not.toHaveTextContent(/42\s949\s672\s960/);
    expect(follows).toEqual([]);
  });

  // req: EF-BIB-030, EF-USR-022
  it('asks to confirm the identity when the server says the session is too old, then goes on', async () => {
    let n = 0;
    server.use(
      http.post(`${API}/auth/reauth`, () => HttpResponse.json({}))
    );
    serve({
      spaceCheck: (count) =>
        count === 1
          ? refusal(60 * GIB, 40 * GIB, 10 * GIB)
          : HttpResponse.json({ fits: true, estimated_bytes: 60 * GIB, free_bytes: 90 * GIB, reserve_bytes: 10 * GIB }),
      trash: () => {
        n += 1;
        return n === 1
          ? problem(401, 'reauth_required', { reauth_methods: ['password'] })
          : HttpResponse.json({ removed: 3, kept: 0, held_irreplaceable: 0, held_other: 0, failed: 0 });
      }
    });
    open();
    const user = userEvent.setup();
    await waitFor(() => expect(summary()).toHaveTextContent(/unités/));
    await user.click(addButton());
    await user.click(await screen.findByRole('button', { name: 'Vider la corbeille' }));
    await user.type(screen.getByRole('textbox', { name: /empty-trash/ }), 'empty-trash');
    await user.click(screen.getByRole('button', { name: 'Vider maintenant' }));

    await user.type(await screen.findByLabelText('Mot de passe'), 'secret');
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    await waitFor(() => expect(trashes).toHaveLength(2)); // the same word, sent again, not lost
    expect(trashes[1]!.body).toEqual({ typed: 'empty-trash' });
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Corbeille vidée'));
  });

  // req: EF-BIB-030
  it('shows the problem of a trash that could not be emptied, and keeps the refusal and the action', async () => {
    serve({
      spaceCheck: () => refusal(60 * GIB, 40 * GIB, 10 * GIB),
      trash: () => problem(403, 'credential_not_allowed')
    });
    open();
    const user = userEvent.setup();
    await waitFor(() => expect(summary()).toHaveTextContent(/unités/));
    await user.click(addButton());
    await user.click(await screen.findByRole('button', { name: 'Vider la corbeille' }));
    await user.type(screen.getByRole('textbox', { name: /empty-trash/ }), 'empty-trash');
    await user.click(screen.getByRole('button', { name: 'Vider maintenant' }));
    await waitFor(() => expect(screen.getAllByRole('alert').map((a) => a.textContent).join(' ')).toContain('Titre credential_not_allowed'));
    expect(screen.getByRole('button', { name: 'Vider la corbeille' })).toBeInTheDocument();
    expect(spaceChecks).toHaveLength(1);
  });

  // req: EF-UX-006
  it('says it is counting while the estimate is on its way, and adds nothing until it is there', async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    serve({
      estimate: async (url) => {
        await gate;
        return HttpResponse.json(estimateFor(url));
      }
    });
    open();
    expect(await screen.findByRole('status')).toHaveTextContent('Calcul de la taille');
    expect(addButton()).toBeDisabled();
    expect(screen.queryByTestId('add-summary')).toBeNull();
    release();
    await waitFor(() => expect(summary()).toHaveTextContent(/1\s000\sunités/));
    expect(addButton()).toBeEnabled();
    expect(screen.queryByText('Calcul de la taille')).toBeNull();
  });

  // req: EF-UX-006, EF-UX-003
  it('shows the problem of an estimate that failed, adds nothing, and asks again on request', async () => {
    let calls = 0;
    serve({
      estimate: (url) => {
        calls += 1;
        return calls === 1 ? problem(502, 'source.unavailable') : HttpResponse.json(estimateFor(url));
      }
    });
    open();
    const user = userEvent.setup();
    expect(await screen.findByRole('alert')).toHaveTextContent('Titre source.unavailable');
    expect(addButton()).toBeDisabled();
    expect(screen.queryByTestId('add-summary')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Réessayer' }));
    await waitFor(() => expect(summary()).toHaveTextContent(/1\s000\sunités/));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(addButton()).toBeEnabled();
  });

  // req: EF-UX-006, EF-UX-003
  it('shows the problem when the shelves cannot be read, and does not add to a shelf nobody chose', async () => {
    serve({ shelves: () => problem(500, 'internal') });
    open();
    expect(await screen.findByRole('alert')).toHaveTextContent('Titre internal');
    expect(addButton()).toBeDisabled();
    expect(follows).toEqual([]);
  });

  // req: ENF-A11Y-002
  it('is a labelled dialog: focus goes in, Escape and « Fermer » close it, every control has a name', async () => {
    serve();
    const { onclose } = open();
    const dialog = await sheet();
    await waitFor(() => expect(summary()).toHaveTextContent(/unités/));
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    for (const control of within(dialog).getAllByRole('radio')) {
      expect(control).toHaveAccessibleName();
    }
    expect(within(dialog).getByRole('combobox')).toHaveAccessibleName('Étagère');
    const user = userEvent.setup();
    await user.keyboard('{Escape}');
    expect(onclose).toHaveBeenCalledOnce();
    await user.click(within(dialog).getByRole('button', { name: 'Fermer' }));
    expect(onclose).toHaveBeenCalledTimes(2);
  });

  // req: EF-UX-006
  it('never says "Exporter": it is not a family verb, and the sheet is the family\'s', async () => {
    serve({ spaceCheck: () => refusal(60 * GIB, 40 * GIB, 10 * GIB) });
    open();
    await waitFor(() => expect(summary()).toHaveTextContent(/unités/));
    await userEvent.setup().click(addButton());
    await screen.findByRole('alert');
    expect(document.body.textContent).not.toMatch(/export/i);
  });

  // req: ENF-I18N-001
  it('speaks English once the language is English, with nothing left in French', async () => {
    serve({ spaceCheck: () => refusal(60 * GIB, 40 * GIB, 10 * GIB) });
    try {
      setUiLanguage('en');
      open();
      await waitFor(() => expect(summary()).toHaveTextContent(/1,000 units, 62,914,560 bytes/));
      const dialog = screen.getByRole('dialog', { name: /^Add to my library/ });
      expect(within(dialog).getByRole('group', { name: 'What to add' })).toBeInTheDocument();
      expect(within(dialog).getByRole('radio', { name: 'All' })).toBeChecked();
      expect(within(dialog).getByRole('radio', { name: 'From number' })).toBeInTheDocument();
      expect(within(dialog).getByRole('radio', { name: 'New only' })).toBeInTheDocument();
      expect(within(dialog).getByRole('combobox', { name: 'Shelf' })).toBeInTheDocument();
      await userEvent.setup().click(within(dialog).getByRole('button', { name: 'Add to my library' }));
      const alert = await screen.findByRole('alert');
      expect(within(alert).getByRole('button', { name: 'Empty the trash' })).toBeInTheDocument();
      expect(document.body.textContent).not.toMatch(/Ajouter|Étagère|Tout\b|Vider|unités|octets/);
    } finally {
      setUiLanguage('fr');
    }
  });
});
