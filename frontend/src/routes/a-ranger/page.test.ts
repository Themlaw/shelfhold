import { render, screen } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('À ranger', () => {
  // req: EF-EXP-046
  it('test_to_shelve_view_lists_unshelved_editions', async () => {
    server.use(
      http.get(`${BASE}/api/v1/to-shelve`, () =>
        HttpResponse.json({
          items: [
            { edition_id: 'E1', work_id: 'W1', title: '<b>Un EPUB</b>', lang: 'fr', media: 'text' }
          ]
        })
      )
    );
    const { container } = render(Page);
    expect(await screen.findByText('<b>Un EPUB</b>')).toBeInTheDocument();
    expect(container.querySelector('b')).toBeNull();
    expect(screen.getByRole('heading', { level: 1, name: 'À ranger' })).toBeInTheDocument();
  });

  // req: EF-EXP-046
  it('says so when nothing waits, and when there is no session', async () => {
    server.use(http.get(`${BASE}/api/v1/to-shelve`, () => HttpResponse.json({ items: [] })));
    render(Page);
    expect(await screen.findByText('Aucune édition à ranger.')).toBeInTheDocument();
  });

  // req: EF-USR-015
  it('asks for a session on a 401', async () => {
    server.use(
      http.get(`${BASE}/api/v1/to-shelve`, () =>
        HttpResponse.json({ type: 'x', title: 'x', status: 401, code: 'unauthenticated' }, { status: 401 })
      )
    );
    render(Page);
    expect(await screen.findByRole('alert')).toHaveTextContent('Connexion requise');
  });
});
