import { render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();
const ID = '01SELECTIONAAAAAAAAAAAAAAA';
const row = {
  id: ID,
  name: 'À lire',
  query: { reading_state: ['unread'] },
  valid: true,
  created_at: 'x',
  updated_at: 'x'
};
const detail = (items: unknown[], total: number) => ({
  ...row,
  items,
  total,
  capped: false
});

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Saved selections screen', () => {
  // req: EF-BIB-017
  it('test_saved_selections', async () => {
    const user = userEvent.setup();
    server.use(
      http.get(`${BASE}/api/v1/selections`, () => HttpResponse.json({ items: [row] })),
      http.get(`${BASE}/api/v1/selections/${ID}`, () =>
        HttpResponse.json(
          detail(
            [
              {
                work_id: 'W1',
                title: 'Titre 1',
                kind: 'manga',
                edition_id: null,
                cover_hash: null
              }
            ],
            1
          )
        )
      )
    );
    render(Page);
    await user.click(await screen.findByRole('button', { name: 'À lire' }));
    expect(await screen.findByText(/1 (Work|Œuvre)/)).toBeTruthy();
    // the composed cover is decorative: the title is read once, from the caption
    expect(screen.getAllByText('Titre 1').some((node) => node.classList.contains('label'))).toBe(true);
  });

  // req: EF-BIB-017
  it('test_selection_query_grammar_closed', async () => {
    const user = userEvent.setup();
    const seen: unknown[] = [];
    server.use(
      http.get(`${BASE}/api/v1/selections`, () => HttpResponse.json({ items: [] })),
      http.post(`${BASE}/api/v1/selections`, async ({ request }) => {
        seen.push(await request.json());
        return HttpResponse.json({ id: ID }, { status: 201 });
      })
    );
    render(Page);
    await screen.findByText(/No selection yet|Aucune sélection/);
    await user.type(screen.getByLabelText(/Name|Nom/), 'Mes manga');
    await user.click(screen.getByLabelText(/Unread|Non lu/));
    await user.type(screen.getByLabelText(/Kinds|Types/), "manga'; DROP TABLE work");
    await user.click(screen.getByRole('button', { name: /Save the selection|Enregistrer/ }));
    await waitFor(() => expect(seen.length).toBe(1));
    // only the keys of the grammar are ever sent; the server refuses what is not a plain word
    const body = seen[0] as { name: string; query: Record<string, unknown> };
    expect(Object.keys(body)).toEqual(['name', 'query']);
    expect(
      Object.keys(body.query).every((k) =>
        ['reading_state', 'kind', 'shelf', 'lang', 'added_within_days'].includes(k)
      )
    ).toBe(true);
  });
});
