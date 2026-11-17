import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();

const state = (destinations = [{ id: 'D1', target_id: 'T1', adopted: false, linked: false }]) =>
  http.get(`${BASE}/api/v1/setup/state`, () =>
    HttpResponse.json({ targets: [], destinations, first_run: false })
  );
const previewOut = (over: Record<string, unknown> = {}) => ({
  fingerprint: 'f'.repeat(64),
  library_ref: '7',
  series: 2,
  files: 5,
  child_library: false,
  to_check: [],
  download_dir: null,
  roots: [],
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

describe('Réglages : adoption', () => {
  // req: EF-EXP-045
  it('test_adoption_screen_lists_the_files_to_check_with_a_reason_and_adopts_them_all', async () => {
    let confirmed: Record<string, unknown> | null = null;
    server.use(
      state(),
      http.post(`${BASE}/api/v1/setup/adoption/preview`, () =>
        HttpResponse.json(
          previewOut({
            to_check: [
              { path: 'Serie/Serie - Ch.2.cbz', reason: 'image.too_large' },
              { path: 'Serie/Serie - Ch.3.cbz', reason: 'archive.ratio' },
              { path: 'Serie/Serie - Ch.4.cbz', reason: 'something.new' }
            ]
          })
        )
      ),
      http.post(`${BASE}/api/v1/setup/adoption/confirm`, async ({ request }) => {
        confirmed = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          adopted_files: 5,
          already_adopted: 0,
          series: 2,
          library_ref: '7',
          linked: true
        });
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Voir ce qui serait adopté' }));
    expect(await screen.findByText('2 séries et 5 fichiers seraient adoptés.')).toBeInTheDocument();
    const section = screen.getByRole('heading', { name: '3 fichiers à vérifier' }).closest('section')!;
    expect(within(section).getByText('Serie/Serie - Ch.2.cbz')).toBeInTheDocument();
    expect(within(section).getByText(/image trop grande/)).toBeInTheDocument();
    expect(within(section).getByText(/taux de compression suspect/)).toBeInTheDocument();
    expect(within(section).getByText(/refusé par le bac à sable/)).toBeInTheDocument(); // unknown code
    expect(within(section).getByText(/jamais touchés/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Adopter cette bibliothèque' }));
    expect(await screen.findByText('5 fichiers adoptés.')).toBeInTheDocument();
    expect(confirmed).toEqual({
      destination_id: 'D1',
      fingerprint: 'f'.repeat(64),
      library_ref: '7'
    });
  });

  // req: EF-EXP-045, ENF-SEC-003
  it('test_adoption_screen_shows_paths_and_codes_from_the_server_as_text_never_markup', async () => {
    const hostile = '<img src=x onerror=alert(1)>.cbz';
    server.use(
      state(),
      http.post(`${BASE}/api/v1/setup/adoption/preview`, () =>
        HttpResponse.json(previewOut({ to_check: [{ path: hostile, reason: '<b>x</b>' }] }))
      )
    );
    const { container } = render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Voir ce qui serait adopté' }));
    expect(await screen.findByText(hostile)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('li b')).toBeNull();
  });

  // req: EF-EXP-045
  it('test_adoption_screen_without_file_to_check_shows_no_such_section', async () => {
    server.use(
      state(),
      http.post(`${BASE}/api/v1/setup/adoption/preview`, () => HttpResponse.json(previewOut()))
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Voir ce qui serait adopté' }));
    await screen.findByText('2 séries et 5 fichiers seraient adoptés.');
    expect(screen.queryByRole('heading', { name: /à vérifier/ })).toBeNull();
  });

  // req: EF-EXP-045
  it('test_adoption_screen_without_a_destination_offers_no_action', async () => {
    server.use(state([]));
    render(Page);
    expect(await screen.findByText(/Aucune destination n'existe encore/)).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });

  // req: EF-EXP-045
  it('test_adoption_screen_refused_to_a_member_shows_the_problem_and_nothing_else', async () => {
    server.use(
      state(),
      http.post(`${BASE}/api/v1/setup/adoption/preview`, () =>
        HttpResponse.json(
          { type: '/api/v1/problems/forbidden', title: 'Refusé.', status: 403, code: 'forbidden' },
          { status: 403 }
        )
      )
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Voir ce qui serait adopté' }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Adopter cette bibliothèque' })).toBeNull();
  });
});

describe('Réglages : adoption, aperçu en tâche de fond', () => {
  // req: EF-EXP-045
  it('test_a_long_preview_is_followed_until_its_report_and_says_it_is_reading', async () => {
    let polls = 0;
    server.use(
      state(),
      http.post(`${BASE}/api/v1/setup/adoption/preview`, () =>
        HttpResponse.json(
          { job_id: 'J1', status_url: '/api/v1/setup/adoption/preview/J1' },
          { status: 202 }
        )
      ),
      http.get(`${BASE}/api/v1/setup/adoption/preview/J1`, () => {
        polls += 1;
        return HttpResponse.json(
          polls < 2
            ? { status: 'running', job_id: 'J1', preview: null }
            : { status: 'done', job_id: 'J1', preview: previewOut({ files: 9 }) }
        );
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Voir ce qui serait adopté' }));
    expect(await screen.findByText(/Lecture de la bibliothèque en cours/)).toBeInTheDocument();
    expect(
      await screen.findByText('2 séries et 9 fichiers seraient adoptés.', {}, { timeout: 10000 })
    ).toBeInTheDocument();
    expect(polls).toBe(2);
    expect(screen.queryByText(/Lecture de la bibliothèque en cours/)).toBeNull();
  }, 20000);

  // req: EF-EXP-045
  it('test_the_screen_says_that_cbr_cb7_and_pdf_are_not_probed', async () => {
    server.use(
      state(),
      http.post(`${BASE}/api/v1/setup/adoption/preview`, () => HttpResponse.json(previewOut()))
    );
    render(Page);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Voir ce qui serait adopté' }));
    expect(await screen.findByText(/CBR, CB7 et PDF ne le sont pas/)).toBeInTheDocument();
  });
});
