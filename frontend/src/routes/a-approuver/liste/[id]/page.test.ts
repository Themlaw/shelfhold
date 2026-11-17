import { render, screen, within } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import Page from './+page.svelte';

vi.mock('$app/state', () => ({ page: { params: { id: '01JLIST0000000000000000000' } } }));

// The one review screen of an imported list (EF-USR-020): `GET /api/v1/request-lists/{id}`.
const BASE = 'http://shelfhold.test';
const API = `${BASE}/api/v1/request-lists/01JLIST0000000000000000000`;
const server = setupServer();
const line = (position: number, text: string | null, over: Record<string, unknown> = {}) => ({
  position, text, isbn: null, outcome: 'requested', annotated: false, request_id: `01JREQ0000000000000000000${position}`,
  request_state: 'pending', needs_age: false, in_library: false, ...over
});
const review = (items: unknown[], over: Record<string, unknown> = {}) => ({
  id: '01JLIST0000000000000000000', name: 'Été', user_id: 'U', user_role: 'admin',
  created_at: '2026-11-10T09:00:00Z', collection_id: null, in_library: 1, requested: 2, annotated: 0, items, ...over
});

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

describe('Imported list review', () => {
  // req: EF-USR-020
  it('draws every line in order, one screen, with what is in the library and what waits', async () => {
    server.use(
      http.get(API, () =>
        HttpResponse.json(
          review([
            line(0, 'Un livre de la bibliothèque', { outcome: 'in_library', request_id: null, request_state: null, in_library: true }),
            line(1, 'Un livre à demander'),
            line(2, 'Un livre refusé', { request_state: 'rejected' })
          ])
        )
      )
    );
    render(Page);
    expect(await screen.findByRole('heading', { name: 'Été', level: 1 })).toBeInTheDocument();
    const rows = within(screen.getByRole('list')).getAllByRole('listitem') as [HTMLElement, HTMLElement, HTMLElement];
    expect(rows).toHaveLength(3);
    expect(within(rows[0]).getByText('Dans la bibliothèque, sur l’étagère de lecture')).toBeInTheDocument();
    expect(within(rows[1]).getByText('Demandé, en attente')).toBeInTheDocument();
    expect(within(rows[1]).getByRole('button', { name: 'Décider' })).toBeInTheDocument();
    expect(within(rows[2]).getByText('Refusé')).toBeInTheDocument();
    expect(within(rows[2]).queryByRole('button', { name: 'Décider' })).toBeNull(); // already decided
  });

  // req: EF-USR-020
  it('warns, without refusing, about a line that asks for a precise annotated edition', async () => {
    server.use(
      http.get(API, () =>
        HttpResponse.json(review([line(0, 'Un livre (édition annotée)', { annotated: true })], { annotated: 1 }))
      )
    );
    render(Page);
    expect(await screen.findByText(/1 ligne réclame une édition annotée précise/)).toBeInTheDocument();
    expect(screen.getByText('Édition annotée demandée')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Décider' })).toBeInTheDocument(); // a warning only
  });

  // req: EF-USR-020, EF-USR-010
  it("flags every line of a child's list as needing an age at approval", async () => {
    server.use(
      http.get(API, () =>
        HttpResponse.json(review([line(0, 'Un livre', { needs_age: true })], { user_role: 'child' }))
      )
    );
    render(Page);
    expect(await screen.findByText('Âge à choisir')).toBeInTheDocument();
    expect(screen.getByText(/chaque demande demandera un âge/)).toBeInTheDocument();
  });

  // req: EF-USR-020
  it("shows only the totals of another adult's private list", async () => {
    server.use(
      http.get(API, () =>
        HttpResponse.json(review([line(0, null), line(1, null)], { user_role: 'member', requested: 2, in_library: 0 }))
      )
    );
    render(Page);
    expect(await screen.findByText(/seuls les totaux sont montrés/)).toBeInTheDocument();
    expect(screen.getAllByText('Titre masqué')).toHaveLength(2);
  });

  // req: EF-USR-020
  it('shows the same not found for a list that is not the reader’s', async () => {
    server.use(
      http.get(API, () =>
        HttpResponse.json({ type: '/api/v1/problems/request.not_found', title: 'Introuvable', status: 404, code: 'request.not_found' }, { status: 404 })
      )
    );
    render(Page);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByRole('list')).toBeNull();
  });
});
