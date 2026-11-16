import { render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import RelatedWorks from './RelatedWorks.svelte';

const BASE = 'http://shelfhold.test';
const API = `${BASE}/api/v1/works/W1/relations`;
const server = setupServer();

const item = (workId: string, title: string, kind: string, outgoing: boolean) => ({
  work_id: workId,
  title,
  kind,
  outgoing,
  edition_id: `E-${workId}`,
  cover_hash: null
});
const answer = (items: unknown[]) =>
  http.get(API, () => HttpResponse.json({ work_id: 'W1', count: items.length, items }));

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

describe('Related works', () => {
  // req: EF-BIB-039
  it('test_relation_visible_on_both_sheets', async () => {
    server.use(
      answer([
        item('W2', 'La suite', 'sequel_of', false),
        item('W3', "L'œuvre d'origine", 'adaptation_of', true)
      ])
    );
    render(RelatedWorks, { workId: 'W1', canEdit: false });
    // each relation is read from the side of this Work, with the other Work's title as a link
    const sequel = await screen.findByRole('link', { name: 'La suite' });
    expect(sequel).toHaveAttribute('href', '/serie/E-W2');
    expect(screen.getByRole('link', { name: "L'œuvre d'origine" })).toHaveAttribute(
      'href',
      '/serie/E-W3'
    );
    expect(screen.getByText('Suite de cette œuvre')).toBeInTheDocument();
    expect(screen.getByText('Adaptation de')).toBeInTheDocument();
    // no action is drawn for a reader who may not edit
    expect(screen.queryByRole('button', { name: /Retirer/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /Lier/ })).toBeNull();
  });

  // req: EF-BIB-039
  it('test_a_work_with_no_visible_relation_draws_nothing', async () => {
    server.use(answer([]));
    const { container } = render(RelatedWorks, { workId: 'W1', canEdit: false });
    await waitFor(() => expect(container.querySelector('section')).toBeNull());
  });

  // req: EF-BIB-039
  it('test_the_admin_removes_a_relation_and_the_list_is_read_again', async () => {
    let removed = false;
    server.use(
      http.get(API, () =>
        HttpResponse.json({
          work_id: 'W1',
          count: removed ? 0 : 1,
          items: removed ? [] : [item('W2', 'La suite', 'sequel_of', true)]
        })
      ),
      http.delete(`${API}/sequel_of/W2`, ({ request }) => {
        expect(request.headers.get('x-csrf-token')).toBe('tok123');
        removed = true;
        return new HttpResponse(null, { status: 204 });
      })
    );
    render(RelatedWorks, { workId: 'W1', canEdit: true });
    await userEvent.click(await screen.findByRole('button', { name: /Retirer/ }));
    await waitFor(() => expect(screen.queryByRole('link', { name: 'La suite' })).toBeNull());
    expect(removed).toBe(true);
  });

  // req: EF-BIB-039
  it('test_the_admin_relates_a_work_found_by_its_title', async () => {
    let posted: unknown = null;
    server.use(
      answer([]),
      http.get(`${BASE}/api/v1/search`, () =>
        HttpResponse.json({
          items: [{ work_id: 'W9', title: 'Une autre histoire', kind: 'manga' }],
          total: 1
        })
      ),
      http.post(API, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ created: true }, { status: 201 });
      })
    );
    render(RelatedWorks, { workId: 'W1', canEdit: true });
    await userEvent.type(await screen.findByRole('searchbox'), 'autre');
    await userEvent.click(await screen.findByRole('button', { name: /Une autre histoire/ }));
    await userEvent.click(screen.getByRole('button', { name: /Lier/ }));
    await waitFor(() => expect(posted).toEqual({ to_work_id: 'W9', kind: 'sequel_of' }));
  });

  // req: EF-BIB-039
  it('test_a_refusal_of_the_server_is_shown_and_nothing_is_added', async () => {
    server.use(
      answer([]),
      http.get(`${BASE}/api/v1/search`, () =>
        HttpResponse.json({
          items: [{ work_id: 'W9', title: 'Une autre histoire', kind: 'manga' }],
          total: 1
        })
      ),
      http.post(API, () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'cycle', status: 409, code: 'relation.cycle' },
          { status: 409, headers: { 'content-type': 'application/problem+json' } }
        )
      )
    );
    render(RelatedWorks, { workId: 'W1', canEdit: true });
    await userEvent.type(await screen.findByRole('searchbox'), 'autre');
    await userEvent.click(await screen.findByRole('button', { name: /Une autre histoire/ }));
    await userEvent.click(screen.getByRole('button', { name: /Lier/ }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });
});
