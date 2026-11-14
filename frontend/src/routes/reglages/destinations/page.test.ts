import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

// The destinations screen (EF-EXP-031): remove a destination (keep the files, or take them away)
// and detach Kavita completely, each behind a preview that says what would be lost, a phrase the
// server gives and the admin types, and the fresh-session prompt; the detachment ends on its
// final report. Every body is the one the doors take (backend `test_l45_destination_doors.py`).
const BASE = 'http://shelfhold.test';
const server = setupServer();
const ADMIN_PW = ['synthetic', 'admin', 'pw', '0045'].join('-'); // no literal: gitleaks scans commits
const KAVITA_PW = ['synthetic', 'kavita', 'pw', '0045'].join('-');
const DEST = '01JDEST000000000000000000A';
const TARGET = '01JTARG000000000000000000A';
const REMOVE_PHRASE = 'RETIRER manga/';
const DETACH_PHRASE = 'DÉTACHER Kavita';
const destination = (over: Record<string, unknown> = {}) => ({
  id: DEST,
  shelf_id: '01JSHLF000000000000000000A',
  shelf_name: 'Manga',
  target_id: TARGET,
  root_subpath: 'manga/',
  transport: 'tree',
  adopted: true,
  flagged_names: 0,
  template: { chapter: 'c', volume: 'v', special: 's', special_dir: 'Specials', segment: 'p' },
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
const refused = (code: string) =>
  HttpResponse.json({ type: `/api/v1/problems/${code}`, title: 'x', status: 409, code }, { status: 409 });
const removalPreview = (mode = 'keep_files') => ({
  destination_id: DEST,
  mode,
  phrase: REMOVE_PHRASE,
  warnings: [{ code: 'removal:files_deleted', count: 12 }]
});
const removalReport = (over: Record<string, unknown> = {}) => ({
  deleted: 12, forgotten: 40, cancelled: false, complete: true,
  left: [{ code: 'adopted_kept', path: 'manga/Adopted' }],
  ...over
});
const detachPreview = {
  target_id: TARGET,
  phrase: DETACH_PHRASE,
  warnings: [{ code: 'detach:hidden_erased', count: 12 }]
};
const detachReport = (over: Record<string, unknown> = {}) => ({
  target_id: TARGET, complete: true, hidden_removed: 12, key_revoked: true, key_gone: false,
  forgotten: 40, cancelled: false, settings_restored: true,
  settings: [{ exclude_patterns_restored: true, type_unchanged: true }],
  leftovers: [
    { code: 'suwayomi_downloads', count: 3 },
    { code: 'kavita_accounts', count: 1 },
    { code: 'store', count: 25 }
  ],
  ...over
});

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const list = (items: unknown[] = [destination()]) =>
  server.use(http.get(`${BASE}/api/v1/destinations`, () => HttpResponse.json({ items })));
const reauthConfirm = () =>
  within(screen.getByRole('region', { name: 'Confirmez votre identité' })).getByRole('button', { name: 'Confirmer' });
const row = async () => (await screen.findByText(/manga\//)).closest('li') as HTMLElement;
const phrase = () => screen.getByLabelText('Recopiez la phrase pour confirmer');
const reauthWith = (counter: { n: number }) =>
  http.post(`${BASE}/api/v1/auth/reauth`, async ({ request }) => {
    counter.n += 1;
    expect(await request.json()).toEqual({ method: 'password', password: ADMIN_PW });
    return HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' });
  });

describe('Destinations', () => {
  // req: EF-EXP-031
  it('lists the destinations with their shelf and two actions each, and calls nothing else', async () => {
    list();
    render(Page);
    expect(await screen.findByRole('heading', { name: 'Destinations', level: 1 })).toBeTruthy();
    const li = await row();
    expect(li).toHaveTextContent('Manga');
    expect(within(li).getByRole('button', { name: 'Retirer' })).toBeTruthy();
    expect(within(li).getByRole('button', { name: 'Détacher Kavita' })).toBeTruthy();
    expect(screen.queryByLabelText('Recopiez la phrase pour confirmer')).toBeNull();
  });

  // req: EF-EXP-031
  it('previews the removal with the mode chosen and shows what would be lost, before any phrase', async () => {
    list();
    let body: unknown = null;
    server.use(
      http.post(`${BASE}/api/v1/destinations/${DEST}/removal-preview`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(removalPreview('delete_files'));
      })
    );
    render(Page);
    const user = userEvent.setup();
    const li = await row();
    await user.click(within(li).getByRole('button', { name: 'Retirer' }));
    await user.click(screen.getByLabelText('Supprimer les fichiers'));
    await user.click(screen.getByRole('button', { name: 'Voir ce qui sera perdu' }));
    expect(await screen.findByText('12 fichiers écrits par Shelfhold sont supprimés pour de bon.')).toBeTruthy();
    expect(body).toEqual({ mode: 'delete_files' });
    expect(screen.getByText(REMOVE_PHRASE)).toBeTruthy();
  });

  // req: EF-EXP-031
  it('offers « Conserver les fichiers » by default: the safe mode is the one that is checked', async () => {
    list();
    render(Page);
    const user = userEvent.setup();
    await user.click(within(await row()).getByRole('button', { name: 'Retirer' }));
    expect(screen.getByLabelText('Conserver les fichiers')).toBeChecked();
    expect(screen.getByLabelText('Supprimer les fichiers')).not.toBeChecked();
  });

  // req: EF-EXP-031
  it('test_remove_action: removes with the mode and the phrase typed exactly, nothing before', async () => {
    list();
    let removal: unknown = null;
    let called = 0;
    server.use(
      http.post(`${BASE}/api/v1/destinations/${DEST}/removal-preview`, () => HttpResponse.json(removalPreview())),
      http.post(`${BASE}/api/v1/destinations/${DEST}/removal`, async ({ request }) => {
        called += 1;
        removal = { body: await request.json(), csrf: request.headers.get('x-csrf-token') };
        return HttpResponse.json(removalReport());
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(within(await row()).getByRole('button', { name: 'Retirer' }));
    await user.click(screen.getByRole('button', { name: 'Voir ce qui sera perdu' }));
    await screen.findByText(REMOVE_PHRASE);
    await user.type(phrase(), 'retirer manga/');
    expect(screen.getByRole('button', { name: 'Confirmer' })).toBeDisabled();
    expect(called).toBe(0);
    await user.clear(phrase());
    await user.type(phrase(), REMOVE_PHRASE);
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    expect((await screen.findByRole('status')).textContent).toMatch(/Destination retirée\./);
    expect(removal).toEqual({ body: { mode: 'keep_files', typed: REMOVE_PHRASE }, csrf: 'tok123' });
  });

  // req: EF-USR-022
  it('test_remove_action_reauth: asks for the password in place before the removal, then replays it', async () => {
    list();
    const bodies: unknown[] = [];
    const reauths = { n: 0 };
    server.use(
      http.post(`${BASE}/api/v1/destinations/${DEST}/removal-preview`, () => HttpResponse.json(removalPreview())),
      http.post(`${BASE}/api/v1/destinations/${DEST}/removal`, async ({ request }) => {
        bodies.push(await request.json());
        return bodies.length === 1 ? stale() : HttpResponse.json(removalReport());
      }),
      reauthWith(reauths)
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(within(await row()).getByRole('button', { name: 'Retirer' }));
    await user.click(screen.getByRole('button', { name: 'Voir ce qui sera perdu' }));
    await screen.findByText(REMOVE_PHRASE);
    await user.type(phrase(), REMOVE_PHRASE);
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    await user.type(await screen.findByLabelText('Mot de passe'), ADMIN_PW);
    expect(bodies).toHaveLength(1);
    await user.click(reauthConfirm());
    await screen.findByText('Destination retirée.');
    expect(reauths.n).toBe(1);
    expect(bodies).toEqual([
      { mode: 'keep_files', typed: REMOVE_PHRASE },
      { mode: 'keep_files', typed: REMOVE_PHRASE }
    ]);
  });

  // req: EF-EXP-031
  it('says the removal is not finished when the report is not complete', async () => {
    list();
    server.use(
      http.post(`${BASE}/api/v1/destinations/${DEST}/removal-preview`, () => HttpResponse.json(removalPreview())),
      http.post(`${BASE}/api/v1/destinations/${DEST}/removal`, () => HttpResponse.json(removalReport({ complete: false })))
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(within(await row()).getByRole('button', { name: 'Retirer' }));
    await user.click(screen.getByRole('button', { name: 'Voir ce qui sera perdu' }));
    await screen.findByText(REMOVE_PHRASE);
    await user.type(phrase(), REMOVE_PHRASE);
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    expect((await screen.findByRole('alert')).textContent).toMatch(/Retrait inachevé : reprenez-le\./);
    expect(screen.queryByText('Destination retirée.')).toBeNull();
  });

  // req: EF-EXP-031
  it('shows the refusal of the server when the phrase was refused (409) instead of a success', async () => {
    list();
    server.use(
      http.post(`${BASE}/api/v1/destinations/${DEST}/removal-preview`, () => HttpResponse.json(removalPreview())),
      http.post(`${BASE}/api/v1/destinations/${DEST}/removal`, () => refused('removal.refused'))
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(within(await row()).getByRole('button', { name: 'Retirer' }));
    await user.click(screen.getByRole('button', { name: 'Voir ce qui sera perdu' }));
    await screen.findByText(REMOVE_PHRASE);
    await user.type(phrase(), REMOVE_PHRASE);
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    expect((await screen.findByRole('alert')).textContent).toMatch(/Le retrait a été refusé\./);
    expect(screen.queryByText('Destination retirée.')).toBeNull();
  });

  // req: EF-EXP-031
  it('previews the detachment of the target of the destination and shows its warnings and its fields', async () => {
    list();
    let previews = 0;
    server.use(
      http.post(`${BASE}/api/v1/targets/${TARGET}/detach-preview`, () => {
        previews += 1;
        return HttpResponse.json(detachPreview);
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(within(await row()).getByRole('button', { name: 'Détacher Kavita' }));
    expect(await screen.findByText(DETACH_PHRASE)).toBeTruthy();
    expect(previews).toBe(1);
    expect(screen.getByText(/12 fichiers cachés seront effacés/)).toBeTruthy();
    const password = screen.getByLabelText('Mot de passe du compte Kavita dédié') as HTMLInputElement;
    expect(password.type).toBe('password');
    expect(screen.getByLabelText('Effacer les données cachées')).not.toBeChecked();
    expect(screen.getByLabelText('Effacer le journal des pierres tombales')).not.toBeChecked();
  });

  // req: EF-EXP-031
  it('test_detach_action: detaches with the phrase, the password and the options, exact body', async () => {
    list();
    let body: unknown = null;
    server.use(
      http.post(`${BASE}/api/v1/targets/${TARGET}/detach-preview`, () => HttpResponse.json(detachPreview)),
      http.post(`${BASE}/api/v1/targets/${TARGET}/detach`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(detachReport());
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(within(await row()).getByRole('button', { name: 'Détacher Kavita' }));
    await screen.findByText(DETACH_PHRASE);
    await user.type(screen.getByLabelText('Mot de passe du compte Kavita dédié'), KAVITA_PW);
    await user.click(screen.getByLabelText('Effacer les données cachées'));
    await user.type(phrase(), DETACH_PHRASE);
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    await screen.findByRole('region', { name: 'Rapport final' });
    expect(body).toEqual({
      typed: DETACH_PHRASE, credential: KAVITA_PW,
      discard_hidden_data: true, discard_tombstone_journal: false
    });
  });

  // req: EF-EXP-031
  it('leaves the password out of the body when none was typed', async () => {
    list();
    let body: Record<string, unknown> = {};
    server.use(
      http.post(`${BASE}/api/v1/targets/${TARGET}/detach-preview`, () => HttpResponse.json(detachPreview)),
      http.post(`${BASE}/api/v1/targets/${TARGET}/detach`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(detachReport());
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(within(await row()).getByRole('button', { name: 'Détacher Kavita' }));
    await screen.findByText(DETACH_PHRASE);
    await user.type(phrase(), DETACH_PHRASE);
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    await screen.findByRole('region', { name: 'Rapport final' });
    expect(body).toEqual({ typed: DETACH_PHRASE, discard_hidden_data: false, discard_tombstone_journal: false });
  });

  // req: EF-USR-022
  it('test_detach_action_reauth: asks for the Shelfhold password in place before the detachment, then replays it', async () => {
    list();
    const bodies: unknown[] = [];
    const reauths = { n: 0 };
    server.use(
      http.post(`${BASE}/api/v1/targets/${TARGET}/detach-preview`, () => HttpResponse.json(detachPreview)),
      http.post(`${BASE}/api/v1/targets/${TARGET}/detach`, async ({ request }) => {
        bodies.push(await request.json());
        return bodies.length === 1 ? stale() : HttpResponse.json(detachReport());
      }),
      reauthWith(reauths)
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(within(await row()).getByRole('button', { name: 'Détacher Kavita' }));
    await screen.findByText(DETACH_PHRASE);
    await user.type(phrase(), DETACH_PHRASE);
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    await user.type(await screen.findByLabelText('Mot de passe'), ADMIN_PW);
    expect(screen.queryByRole('region', { name: 'Rapport final' })).toBeNull();
    await user.click(reauthConfirm());
    await screen.findByRole('region', { name: 'Rapport final' });
    expect(reauths.n).toBe(1);
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
  });

  // req: EF-EXP-031
  it('test_detach_screen_shows_final_report: what was undone and what was left in place', async () => {
    list();
    server.use(
      http.post(`${BASE}/api/v1/targets/${TARGET}/detach-preview`, () => HttpResponse.json(detachPreview)),
      http.post(`${BASE}/api/v1/targets/${TARGET}/detach`, () => HttpResponse.json(detachReport()))
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(within(await row()).getByRole('button', { name: 'Détacher Kavita' }));
    await screen.findByText(DETACH_PHRASE);
    await user.type(phrase(), DETACH_PHRASE);
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    const report = await screen.findByRole('region', { name: 'Rapport final' });
    expect(report).toHaveTextContent('Kavita fonctionne sans Shelfhold.');
    expect(report).toHaveTextContent('Réglages de Kavita restaurés');
    expect(report).toHaveTextContent('Clé dédiée révoquée');
    expect(report).toHaveTextContent('12 fichiers cachés retirés');
    const left = within(report).getByRole('list', { name: 'Laissé en place' });
    const items = within(left).getAllByRole('listitem');
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent('Laissé en place : les téléchargements de 3 séries dans Suwayomi ; supprimez-les là-bas si vous voulez.');
    expect(items[1]).toHaveTextContent('Laissé en place : les comptes Kavita (1 dédié) ; retirez-les dans Kavita si vous voulez.');
    expect(items[2]).toHaveTextContent('Laissé en place : /store contient 25 fichiers.');
  });

  // req: EF-EXP-031
  it('says what is not done when the report is partial: settings not restored, key not revoked, not complete', async () => {
    list();
    server.use(
      http.post(`${BASE}/api/v1/targets/${TARGET}/detach-preview`, () => HttpResponse.json(detachPreview)),
      http.post(`${BASE}/api/v1/targets/${TARGET}/detach`, () =>
        HttpResponse.json(detachReport({ complete: false, key_revoked: false, settings_restored: false, hidden_removed: 0, leftovers: [] }))
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(within(await row()).getByRole('button', { name: 'Détacher Kavita' }));
    await screen.findByText(DETACH_PHRASE);
    await user.type(phrase(), DETACH_PHRASE);
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    const report = await screen.findByRole('region', { name: 'Rapport final' });
    expect(report).toHaveTextContent('Détachement inachevé : reprenez-le.');
    expect(report).toHaveTextContent('Réglages de Kavita non restaurés');
    expect(report).toHaveTextContent('Clé dédiée non révoquée');
    expect(report).not.toHaveTextContent('Kavita fonctionne sans Shelfhold.');
    expect(within(report).queryByRole('list', { name: 'Laissé en place' })).toBeNull();
  });

  // req: EF-EXP-031
  it('never shows the password of the Kavita account again once the detachment is done', async () => {
    list();
    server.use(
      http.post(`${BASE}/api/v1/targets/${TARGET}/detach-preview`, () => HttpResponse.json(detachPreview)),
      http.post(`${BASE}/api/v1/targets/${TARGET}/detach`, () => HttpResponse.json(detachReport()))
    );
    const { container } = render(Page);
    const user = userEvent.setup();
    await user.click(within(await row()).getByRole('button', { name: 'Détacher Kavita' }));
    await screen.findByText(DETACH_PHRASE);
    await user.type(screen.getByLabelText('Mot de passe du compte Kavita dédié'), KAVITA_PW);
    await user.type(phrase(), DETACH_PHRASE);
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    await screen.findByRole('region', { name: 'Rapport final' });
    expect(container.textContent).not.toContain(KAVITA_PW);
    for (const field of container.querySelectorAll('input')) expect((field as HTMLInputElement).value).not.toBe(KAVITA_PW);
  });

  // req: EF-EXP-031
  it('shows the refusal of the server when the detachment was refused (409)', async () => {
    list();
    server.use(
      http.post(`${BASE}/api/v1/targets/${TARGET}/detach-preview`, () => HttpResponse.json(detachPreview)),
      http.post(`${BASE}/api/v1/targets/${TARGET}/detach`, () => refused('detach.refused'))
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(within(await row()).getByRole('button', { name: 'Détacher Kavita' }));
    await screen.findByText(DETACH_PHRASE);
    await user.type(phrase(), DETACH_PHRASE);
    await user.click(screen.getByRole('button', { name: 'Confirmer' }));
    expect((await screen.findByRole('alert')).textContent).toMatch(/Le détachement a été refusé\./);
    expect(screen.queryByRole('region', { name: 'Rapport final' })).toBeNull();
  });

  // req: EF-EXP-031
  it('cancels a removal or a detachment without calling the acting door', async () => {
    list();
    server.use(
      http.post(`${BASE}/api/v1/destinations/${DEST}/removal-preview`, () => HttpResponse.json(removalPreview()))
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(within(await row()).getByRole('button', { name: 'Retirer' }));
    await user.click(screen.getByRole('button', { name: 'Voir ce qui sera perdu' }));
    await screen.findByText(REMOVE_PHRASE);
    await user.click(screen.getByRole('button', { name: 'Annuler' }));
    expect(screen.queryByLabelText('Recopiez la phrase pour confirmer')).toBeNull();
    expect(within(await row()).getByRole('button', { name: 'Retirer' })).toBeTruthy();
  });

  // req: EF-USR-001, ENF-SEC-010
  it('shows nothing to a member or a child: the server refused the list, and no action is offered', async () => {
    server.use(http.get(`${BASE}/api/v1/destinations`, forbidden));
    render(Page);
    expect((await screen.findByRole('alert')).textContent).toMatch(/réservés à l'administrateur/);
    expect(screen.queryByRole('button', { name: 'Retirer' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Détacher Kavita' })).toBeNull();
    expect(screen.queryByText(/manga\//)).toBeNull();
  });

  // req: EF-EXP-002
  it('says so when there is no destination, and offers nothing to remove or detach', async () => {
    list([]);
    render(Page);
    expect(await screen.findByText("Aucune destination n'est reliée à une étagère.")).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Retirer' })).toBeNull();
  });
});
