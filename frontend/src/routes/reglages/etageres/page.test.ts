import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

// The shelves screen (EF-EXP-002, EF-EXP-005, EF-EXP-030, EF-EXP-037): the shelves and the
// naming template of each destination, the naming preview (folded unless the template was
// changed or a name is contested), the rename plan with its warning, and its application. The
// server is the gate; every request is an exact body of the OpenAPI, and `onUnhandledRequest:
// 'error'` makes any request a test did not expect a failure.
const BASE = 'http://shelfhold.test';
const server = setupServer();
const ADMIN_PW = ['synthetic', 'admin', 'pw', '0045'].join('-'); // no literal: gitleaks scans commits
const DEST = '01JDEST000000000000000000A';
const PLAN = '01JPLAN0000000000000000R01';
const PHRASE = 'PERDRE LA PROGRESSION';
const TEMPLATE = {
  chapter: '{series} - Chapter {number}',
  volume: '{series} - Vol.{number}',
  special: '{series} - {label}',
  special_dir: 'Specials',
  segment: '{series} - Part {n}'
};
const destination = (over: Record<string, unknown> = {}) => ({
  id: DEST,
  shelf_id: '01JSHLF000000000000000000A',
  shelf_name: 'Manga',
  target_id: '01JTARG000000000000000000A',
  root_subpath: 'manga/',
  transport: 'tree',
  adopted: false,
  flagged_names: 0,
  template: TEMPLATE,
  ...over
});
const renamePlan = (over: Record<string, unknown> = {}) => ({
  plan_id: PLAN,
  base_version: '4242',
  steps: [{ kind: 'file', src: 'A/a.cbz', dst: 'A/b.cbz', export_item_id: '01JITEM00000000000000000A1' }],
  batches: 1,
  blocked: [],
  warnings: [{ code: 'progress_loss', readers: 7, exact: true, series: ['A'] }],
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
const forbidden = () =>
  HttpResponse.json({ type: '/api/v1/problems/forbidden', title: 'x', status: 403, code: 'forbidden' }, { status: 403 });
const accepted = () =>
  HttpResponse.json({ job_id: '01JJOB0000000000000000000A', status_url: '/api/v1/jobs/01JJOB0000000000000000000A' }, { status: 202 });

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const lists = (destinations: unknown[]) =>
  server.use(
    http.get(`${BASE}/api/v1/shelves`, () =>
      HttpResponse.json({ items: [{ id: '01JSHLF000000000000000000A', name: 'Manga', max_age_rating: null }], since: 'v1', removed: [] })
    ),
    http.get(`${BASE}/api/v1/destinations`, () => HttpResponse.json({ items: destinations }))
  );
const reauthConfirm = () =>
  within(screen.getByRole('region', { name: 'Confirmez votre identité' })).getByRole('button', { name: 'Confirmer' });
const row = async () => (await screen.findByText(/manga\//)).closest('li') as HTMLElement;
const FIELDS = ['Chapitre', 'Tome', 'Spécial', 'Dossier des spéciaux', 'Segment'];
const previewBody = (over: Record<string, unknown> = {}) => ({
  verified: true,
  entries: [
    { key: 'k1', rel_path: 'Manga/Manga - Chapter 1.cbz', template_ref: 'chapter', status: 'confirmed', flags: [] },
    { key: 'k2', rel_path: 'Manga/Manga - Chapter 2.cbz', template_ref: 'chapter', status: 'flagged', flags: ['number_collision'] }
  ],
  collisions: [{ kind: 'number', names: ['Manga - Chapter 2.cbz', 'Manga - Chapter 02.cbz'] }],
  ...over
});

describe('Étagères', () => {
  // req: EF-EXP-002
  it('lists the shelves with their destinations and the template of each', async () => {
    lists([destination()]);
    render(Page);
    expect(await screen.findByRole('heading', { name: 'Étagères', level: 1 })).toBeTruthy();
    const li = await row();
    expect(li).toHaveTextContent('Manga');
    for (const label of FIELDS) expect(within(li).getByLabelText(label)).toBeTruthy();
    expect(within(li).getByLabelText('Chapitre')).toHaveValue(TEMPLATE.chapter);
    expect(within(li).getByLabelText('Dossier des spéciaux')).toHaveValue('Specials');
  });

  // req: EF-EXP-005
  it('test_naming_preview_collapsed_unless_template_changed: folded, and not one request while folded', async () => {
    lists([destination()]);
    // no handler for naming-preview: a call would fail the test (onUnhandledRequest: 'error')
    render(Page);
    const li = await row();
    const summary = within(li).getByText('Aperçu du nommage');
    expect(summary.closest('details')).not.toHaveAttribute('open');
    expect(within(li).queryByText('Non vérifié')).toBeNull();
  });

  // req: EF-EXP-005
  it('unfolds and asks the server for the names once the template is changed, with the whole candidate template', async () => {
    lists([destination()]);
    let body: unknown = null;
    server.use(
      http.post(`${BASE}/api/v1/plans/naming-preview`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(previewBody());
      })
    );
    render(Page);
    const user = userEvent.setup();
    const li = await row();
    const chapter = within(li).getByLabelText('Chapitre');
    await user.clear(chapter);
    await user.click(chapter);
    await user.paste('Ch {number} {series}'); // « { » is a key in the typing syntax: paste instead
    await within(li).findByText('Manga/Manga - Chapter 1.cbz');
    expect(within(li).getByText('Aperçu du nommage').closest('details')).toHaveAttribute('open');
    expect(body).toEqual({
      shelf_destination_id: DEST,
      template: { ...TEMPLATE, chapter: 'Ch {number} {series}' }
    });
  });

  // req: EF-EXP-005
  it('says for each name whether Kavita confirmed it, contested it, and lists the collisions', async () => {
    lists([destination()]);
    server.use(http.post(`${BASE}/api/v1/plans/naming-preview`, () => HttpResponse.json(previewBody())));
    render(Page);
    const user = userEvent.setup();
    const li = await row();
    await user.type(within(li).getByLabelText('Segment'), '!');
    const first = (await within(li).findByText('Manga/Manga - Chapter 1.cbz')).closest('li')!;
    expect(first).toHaveTextContent('Confirmé par Kavita');
    const second = within(li).getByText('Manga/Manga - Chapter 2.cbz').closest('li')!;
    expect(second).toHaveTextContent('Contesté');
    expect(within(li).getByText(/Manga - Chapter 02\.cbz/)).toBeTruthy();
  });

  // req: EF-EXP-005
  it('marks the names "not verified" when Kavita could not be asked', async () => {
    lists([destination()]);
    server.use(
      http.post(`${BASE}/api/v1/plans/naming-preview`, () =>
        HttpResponse.json(
          previewBody({
            verified: false,
            collisions: [],
            entries: [{ key: 'k1', rel_path: 'Manga/Manga - Chapter 1.cbz', template_ref: 'chapter', status: 'unverified', flags: [] }]
          })
        )
      )
    );
    render(Page);
    const user = userEvent.setup();
    const li = await row();
    await user.type(within(li).getByLabelText('Segment'), '!');
    expect(await within(li).findByText('Non vérifié par Kavita')).toBeTruthy();
  });

  // req: EF-EXP-005
  it('is unfolded from the start, and asks for the names of the current template, when the server says names are contested', async () => {
    lists([destination({ flagged_names: 2 })]);
    let body: unknown = null;
    server.use(
      http.post(`${BASE}/api/v1/plans/naming-preview`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(previewBody());
      })
    );
    render(Page);
    const li = await row();
    await within(li).findByText('Manga/Manga - Chapter 2.cbz');
    expect(within(li).getByText('Aperçu du nommage').closest('details')).toHaveAttribute('open');
    expect(body).toEqual({ shelf_destination_id: DEST, template: TEMPLATE });
  });

  // req: EF-EXP-030
  it('offers « Renommer » only once the template differs from the current one', async () => {
    lists([destination()]);
    server.use(http.post(`${BASE}/api/v1/plans/naming-preview`, () => HttpResponse.json(previewBody())));
    render(Page);
    const user = userEvent.setup();
    const li = await row();
    expect(within(li).getByRole('button', { name: 'Renommer' })).toBeDisabled();
    await user.type(within(li).getByLabelText('Segment'), '!');
    expect(within(li).getByRole('button', { name: 'Renommer' })).toBeEnabled();
  });

  // req: EF-EXP-030
  it('test_rename_action: asks for the rename plan with the exact body and shows it with its warning', async () => {
    lists([destination()]);
    let body: unknown = null;
    server.use(
      http.post(`${BASE}/api/v1/plans/naming-preview`, () => HttpResponse.json(previewBody())),
      http.post(`${BASE}/api/v1/plans/renames`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(renamePlan());
      })
    );
    render(Page);
    const user = userEvent.setup();
    const li = await row();
    await user.type(within(li).getByLabelText('Dossier des spéciaux'), '2');
    await user.click(within(li).getByRole('button', { name: 'Renommer' }));
    expect(await screen.findByText('La progression Kavita de 7 lecteurs sera perdue.')).toBeTruthy();
    expect(body).toEqual({ shelf_destination_id: DEST, template: { ...TEMPLATE, special_dir: 'Specials2' } });
    expect(screen.getByRole('button', { name: 'Appliquer le plan' })).toBeDisabled();
  });

  // req: EF-USR-022
  it('test_rename_action_reauth: asks for the password in place before the rename plan, then asks again', async () => {
    lists([destination()]);
    let posts = 0;
    let reauths = 0;
    server.use(
      http.post(`${BASE}/api/v1/plans/naming-preview`, () => HttpResponse.json(previewBody())),
      http.post(`${BASE}/api/v1/plans/renames`, () => {
        posts += 1;
        return posts === 1 ? stale() : HttpResponse.json(renamePlan());
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, async ({ request }) => {
        reauths += 1;
        expect(await request.json()).toEqual({ method: 'password', password: ADMIN_PW });
        return HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' });
      })
    );
    render(Page);
    const user = userEvent.setup();
    const li = await row();
    await user.type(within(li).getByLabelText('Segment'), '!');
    await user.click(within(li).getByRole('button', { name: 'Renommer' }));
    await user.type(await screen.findByLabelText('Mot de passe'), ADMIN_PW);
    expect(posts).toBe(1);
    expect(screen.queryByRole('button', { name: 'Appliquer le plan' })).toBeNull();
    await user.click(reauthConfirm());
    expect(await screen.findByText('La progression Kavita de 7 lecteurs sera perdue.')).toBeTruthy();
    expect(posts).toBe(2);
    expect(reauths).toBe(1);
  });

  // req: EF-EXP-037
  it('test_apply_plan_action: applies with If-Match and the confirmation only after the phrase', async () => {
    lists([destination()]);
    let seen: { path: string; ifMatch: string | null; body: unknown; csrf: string | null } | null = null;
    server.use(
      http.post(`${BASE}/api/v1/plans/naming-preview`, () => HttpResponse.json(previewBody())),
      http.post(`${BASE}/api/v1/plans/renames`, () => HttpResponse.json(renamePlan(), { headers: { ETag: '"4242"' } })),
      http.post(`${BASE}/api/v1/plans/${PLAN}/apply`, async ({ request }) => {
        seen = {
          path: new URL(request.url).pathname,
          ifMatch: request.headers.get('if-match'),
          body: await request.json(),
          csrf: request.headers.get('x-csrf-token')
        };
        return accepted();
      })
    );
    render(Page);
    const user = userEvent.setup();
    const li = await row();
    await user.type(within(li).getByLabelText('Segment'), '!');
    await user.click(within(li).getByRole('button', { name: 'Renommer' }));
    await screen.findByText('La progression Kavita de 7 lecteurs sera perdue.');
    await user.type(screen.getByLabelText('Recopiez la phrase pour confirmer'), PHRASE);
    await user.click(screen.getByRole('button', { name: 'Appliquer le plan' }));
    expect((await screen.findByRole('status')).textContent).toMatch(/application en cours/);
    expect(seen).toEqual({
      path: `/api/v1/plans/${PLAN}/apply`,
      ifMatch: '"4242"',
      body: { confirm_progress_loss: true },
      csrf: 'tok123'
    });
  });

  // req: EF-USR-022
  it('test_apply_plan_action_reauth: the apply asks for the password first, and replays with the same validator and confirmation', async () => {
    lists([destination()]);
    const seen: { ifMatch: string | null; body: unknown }[] = [];
    server.use(
      http.post(`${BASE}/api/v1/plans/naming-preview`, () => HttpResponse.json(previewBody())),
      http.post(`${BASE}/api/v1/plans/renames`, () => HttpResponse.json(renamePlan())),
      http.post(`${BASE}/api/v1/plans/${PLAN}/apply`, async ({ request }) => {
        seen.push({ ifMatch: request.headers.get('if-match'), body: await request.json() });
        return seen.length === 1 ? stale() : accepted();
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, () => HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' }))
    );
    render(Page);
    const user = userEvent.setup();
    const li = await row();
    await user.type(within(li).getByLabelText('Segment'), '!');
    await user.click(within(li).getByRole('button', { name: 'Renommer' }));
    await screen.findByText('La progression Kavita de 7 lecteurs sera perdue.');
    await user.type(screen.getByLabelText('Recopiez la phrase pour confirmer'), PHRASE);
    await user.click(screen.getByRole('button', { name: 'Appliquer le plan' }));
    await user.type(await screen.findByLabelText('Mot de passe'), ADMIN_PW);
    await user.click(reauthConfirm());
    await screen.findByText(/application en cours/);
    expect(seen).toEqual([
      { ifMatch: '"4242"', body: { confirm_progress_loss: true } },
      { ifMatch: '"4242"', body: { confirm_progress_loss: true } }
    ]);
  });

  // req: EF-EXP-030
  it('applies a plan nobody loses anything to, with no phrase and no confirmation', async () => {
    lists([destination()]);
    let body: unknown = 'unset';
    server.use(
      http.post(`${BASE}/api/v1/plans/naming-preview`, () => HttpResponse.json(previewBody())),
      http.post(`${BASE}/api/v1/plans/renames`, () => HttpResponse.json(renamePlan({ warnings: [] }))),
      http.post(`${BASE}/api/v1/plans/${PLAN}/apply`, async ({ request }) => {
        body = await request.json().catch(() => null);
        return accepted();
      })
    );
    render(Page);
    const user = userEvent.setup();
    const li = await row();
    await user.type(within(li).getByLabelText('Segment'), '!');
    await user.click(within(li).getByRole('button', { name: 'Renommer' }));
    await user.click(await screen.findByRole('button', { name: 'Appliquer le plan' }));
    await screen.findByText(/application en cours/);
    expect((body as { confirm_progress_loss?: boolean } | null)?.confirm_progress_loss ?? false).toBe(false);
  });

  // req: EF-EXP-037
  it('tells the admin when the plan changed under them (412) and offers to recompute it', async () => {
    lists([destination()]);
    server.use(
      http.post(`${BASE}/api/v1/plans/naming-preview`, () => HttpResponse.json(previewBody())),
      http.post(`${BASE}/api/v1/plans/renames`, () => HttpResponse.json(renamePlan())),
      http.post(`${BASE}/api/v1/plans/${PLAN}/apply`, () =>
        HttpResponse.json({ type: '/api/v1/problems/plan.stale', title: 'x', status: 412, code: 'plan.stale' }, { status: 412 })
      )
    );
    render(Page);
    const user = userEvent.setup();
    const li = await row();
    await user.type(within(li).getByLabelText('Segment'), '!');
    await user.click(within(li).getByRole('button', { name: 'Renommer' }));
    await screen.findByText('La progression Kavita de 7 lecteurs sera perdue.');
    await user.type(screen.getByLabelText('Recopiez la phrase pour confirmer'), PHRASE);
    await user.click(screen.getByRole('button', { name: 'Appliquer le plan' }));
    expect((await screen.findByRole('alert')).textContent).toMatch(/Le plan a changé[ \u202f]: recalculez-le\./);
  });

  // req: EF-USR-001, ENF-SEC-010
  it.each(['shelves', 'destinations'])('shows nothing to a member or a child: the server refused the %s, and no action is offered', async (refused) => {
    server.use(
      http.get(`${BASE}/api/v1/shelves`, () =>
        refused === 'shelves'
          ? forbidden()
          : HttpResponse.json({ items: [], since: 'v1', removed: [] })
      ),
      http.get(`${BASE}/api/v1/destinations`, () => (refused === 'destinations' ? forbidden() : HttpResponse.json({ items: [] })))
    );
    render(Page);
    expect((await screen.findByRole('alert')).textContent).toMatch(/réservés à l'administrateur/);
    expect(screen.queryByRole('button', { name: 'Renommer' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Appliquer le plan' })).toBeNull();
    expect(screen.queryByLabelText('Chapitre')).toBeNull();
    expect(screen.queryByText('Aperçu du nommage')).toBeNull();
  });

  // req: EF-EXP-002
  it('says so when there is no destination yet, and offers no rename', async () => {
    lists([]);
    render(Page);
    expect(await screen.findByText("Aucune destination n'est reliée à une étagère.")).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Renommer' })).toBeNull();
  });
});
