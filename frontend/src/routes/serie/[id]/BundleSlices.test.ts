import { render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import BundleSlices from './BundleSlices.svelte';

const BASE = 'http://shelfhold.test';
const API = `${BASE}/api/v1/editions/ED1`;
const server = setupServer();

const state = (over: Record<string, unknown> = {}) => ({
  edition_id: 'ED1',
  slice_chapters: null,
  slice_min: 1,
  slice_max: 256,
  parts: [{ index: 1, chapters: 3, first_number: '1', last_number: '3', size: 1200, written: true }],
  excluded: { format: 0, unsafe: 0 },
  can: { set_slice: true },
  ...over
});
const read = (body: unknown, status = 200) =>
  http.get(`${API}/bundle`, () => HttpResponse.json(body as never, { status }));

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

describe('Slices of the series block', () => {
  // req: EF-INT-007
  it('test_bundle_slices_shows_the_parts_in_both_languages', async () => {
    server.use(
      read(
        state({
          slice_chapters: 2,
          parts: [
            { index: 1, chapters: 2, first_number: '1', last_number: '2', size: 900, written: true },
            { index: 2, chapters: 1, first_number: '3', last_number: '3', size: null, written: false }
          ]
        })
      )
    );
    const view = render(BundleSlices, { editionId: 'ED1' });
    expect(await screen.findByRole('heading', { name: 'Bloc pour liseuse' })).toBeInTheDocument();
    expect(screen.getByText('Partie 1 sur 2 : chapitres 1 à 2 (2)')).toBeInTheDocument();
    expect(screen.getByText('Partie 2 sur 2 : chapitre 3 (1)')).toBeInTheDocument();
    view.unmount();
    setUiLanguage('en');
    render(BundleSlices, { editionId: 'ED1' });
    expect(await screen.findByRole('heading', { name: 'E-reader block' })).toBeInTheDocument();
    expect(screen.getByText('Part 1 of 2: chapters 1 to 2 (2)')).toBeInTheDocument();
  });

  // req: EF-INT-007
  it('test_bundle_slices_says_how_many_chapters_are_left_out', async () => {
    server.use(read(state({ excluded: { format: 2, unsafe: 1 } })));
    render(BundleSlices, { editionId: 'ED1' });
    expect(await screen.findByText('3 chapitres hors du bloc (format ou fichier illisible).')).toBeInTheDocument();
  });

  // req: EF-INT-007
  it('test_bundle_slices_sets_the_slice_with_the_csrf_token_and_shows_the_new_parts', async () => {
    let sent: { body: unknown; csrf: string | null } | null = null;
    server.use(
      read(state()),
      http.post(`${API}/bundle-slice`, async ({ request }) => {
        sent = { body: await request.json(), csrf: request.headers.get('X-CSRF-Token') };
        return HttpResponse.json(
          state({
            slice_chapters: 2,
            parts: [
              { index: 1, chapters: 2, first_number: '1', last_number: '2', size: null, written: false },
              { index: 2, chapters: 1, first_number: '3', last_number: '3', size: null, written: false }
            ]
          })
        );
      })
    );
    const user = userEvent.setup();
    render(BundleSlices, { editionId: 'ED1' });
    const field = await screen.findByLabelText('Chapitres par partie');
    await user.type(field, '2');
    await user.click(screen.getByRole('button', { name: 'Enregistrer' }));
    await waitFor(() => expect(sent).not.toBeNull());
    expect(sent).toEqual({ body: { slice_chapters: 2 }, csrf: 'tok123' });
    expect(await screen.findByText('Partie 2 sur 2 : chapitre 3 (1)')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Enregistré.');
  });

  // req: EF-INT-007
  it('test_bundle_slices_clears_the_slice_with_null', async () => {
    let body: unknown = null;
    server.use(
      read(state({ slice_chapters: 2 })),
      http.post(`${API}/bundle-slice`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(state());
      })
    );
    const user = userEvent.setup();
    render(BundleSlices, { editionId: 'ED1' });
    await screen.findByLabelText('Chapitres par partie');
    await user.click(screen.getByRole('button', { name: 'Retirer la tranche' }));
    await waitFor(() => expect(body).toEqual({ slice_chapters: null }));
  });

  // req: EF-INT-007
  it('test_bundle_slices_refuses_a_value_out_of_range_before_asking', async () => {
    let asked = false;
    server.use(
      read(state()),
      http.post(`${API}/bundle-slice`, () => {
        asked = true;
        return HttpResponse.json(state());
      })
    );
    const user = userEvent.setup();
    render(BundleSlices, { editionId: 'ED1' });
    const field = await screen.findByLabelText('Chapitres par partie');
    await user.type(field, '300');
    await user.click(screen.getByRole('button', { name: 'Enregistrer' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('entre 1 et 256');
    expect(asked).toBe(false);
  });

  // req: EF-INT-007
  it('test_bundle_slices_shows_the_refusal_of_the_server', async () => {
    server.use(
      read(state()),
      http.post(`${API}/bundle-slice`, () =>
        HttpResponse.json(
          { type: '/api/v1/problems/forbidden', title: 'Refusé', status: 403 },
          { status: 403 }
        )
      )
    );
    const user = userEvent.setup();
    render(BundleSlices, { editionId: 'ED1' });
    await user.type(await screen.findByLabelText('Chapitres par partie'), '2');
    await user.click(screen.getByRole('button', { name: 'Enregistrer' }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  // req: EF-INT-007
  it('test_bundle_slices_draws_nothing_when_the_server_does_not_allow_it', async () => {
    server.use(read({ type: '/api/v1/problems/forbidden', title: 'x', status: 403 }, 403));
    render(BundleSlices, { editionId: 'ED1' });
    await waitFor(() => expect(document.body.textContent).not.toContain('Bloc pour liseuse'));
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  // req: EF-INT-007
  it('test_bundle_slices_has_no_button_when_set_slice_is_not_allowed', async () => {
    server.use(read(state({ can: { set_slice: false } })));
    render(BundleSlices, { editionId: 'ED1' });
    await screen.findByRole('heading', { name: 'Bloc pour liseuse' });
    expect(screen.queryByRole('button', { name: 'Enregistrer' })).not.toBeInTheDocument();
  });
});
