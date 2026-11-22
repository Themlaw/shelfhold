import { render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage } from '#lib/i18n/index.ts';
import ReadMarks from './ReadMarks.svelte';

const BASE = 'http://shelfhold.test';
const MARKS = `${BASE}/api/reader/read-marks`;
const server = setupServer();

const me = () =>
  http.get(`${BASE}/api/v1/me`, () =>
    HttpResponse.json({ id: 'UADMIN', username: 'admin', role: 'admin', nav: [] })
  );
const mark = (over: Record<string, unknown> = {}) => ({
  number: '3',
  title: 'Le rendez-vous',
  unit_id: 'U1',
  user_id: 'UADMIN',
  username: 'admin',
  ...over
});
const list = (items: unknown[]) => http.get(MARKS, () => HttpResponse.json({ items }));
const problem = (status: number, code: string) =>
  HttpResponse.json({ type: `/api/v1/problems/${code}`, title: code, status, code }, { status });

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

describe('Read marks of the series sheet', () => {
  // req: EF-LEC-014
  it('test_read_marks_lists_the_own_marks_and_the_childrens', async () => {
    server.use(
      me(),
      list([mark(), mark({ number: '5', unit_id: 'U2', user_id: 'UKID', username: 'kid' })])
    );
    render(ReadMarks, { editionId: 'ED1' });
    expect(await screen.findByRole('heading', { name: 'Lectures marquées' })).toBeInTheDocument();
    expect(
      screen.getByText('n° 3 — Le rendez-vous — admin')
    ).toBeInTheDocument();
    expect(screen.getByText('n° 5 — Le rendez-vous — kid')).toBeInTheDocument();
  });

  // req: EF-LEC-014
  it('test_read_marks_says_when_nothing_is_marked', async () => {
    server.use(me(), list([]));
    render(ReadMarks, { editionId: 'ED1' });
    expect(await screen.findByText('Aucune lecture marquée sur cette œuvre.')).toBeInTheDocument();
  });

  // req: EF-LEC-014
  it('test_read_marks_undoes_the_own_mark_through_the_own_door', async () => {
    let csrf: string | null = null;
    server.use(
      me(),
      list([mark()]),
      http.post(`${MARKS}/U1/undo`, ({ request }) => {
        csrf = request.headers.get('X-CSRF-Token');
        return HttpResponse.json({ undone: 1 });
      })
    );
    const user = userEvent.setup();
    render(ReadMarks, { editionId: 'ED1' });
    await user.click(await screen.findByRole('button', { name: 'Annuler la lecture' }));
    await waitFor(() => expect(csrf).toBe('tok123'));
    expect(screen.getByRole('status')).toHaveTextContent('1 marque annulée.');
    expect(screen.queryByRole('button', { name: 'Annuler la lecture' })).not.toBeInTheDocument();
  });

  // req: EF-LEC-014
  it('test_read_marks_undoes_the_mark_of_a_child_through_the_administrators_door', async () => {
    let asked: string | null = null;
    server.use(
      me(),
      list([mark({ number: '5', unit_id: 'U2', user_id: 'UKID', username: 'kid' })]),
      http.post(`${MARKS}/U2/undo/for-child/UKID`, () => {
        asked = 'UKID';
        return HttpResponse.json({ undone: 1 });
      })
    );
    const user = userEvent.setup();
    render(ReadMarks, { editionId: 'ED1' });
    await user.click(await screen.findByRole('button', { name: 'Annuler la lecture' }));
    await waitFor(() => expect(asked).toBe('UKID'));
    expect(screen.getByRole('status')).toHaveTextContent('1 marque annulée.');
  });

  // req: EF-LEC-014
  it('test_read_marks_keeps_the_mark_when_nothing_was_undone', async () => {
    server.use(
      me(),
      list([mark()]),
      http.post(`${MARKS}/U1/undo`, () => HttpResponse.json({ undone: 0 }))
    );
    const user = userEvent.setup();
    render(ReadMarks, { editionId: 'ED1' });
    await user.click(await screen.findByRole('button', { name: 'Annuler la lecture' }));
    expect(await screen.findByRole('status')).toHaveTextContent('0 marque annulée.');
    expect(screen.getByText('n° 3 — Le rendez-vous — admin')).toBeInTheDocument();
  });

  // req: EF-LEC-014
  it('test_read_marks_draws_nothing_when_the_answer_is_not_ours_to_see', async () => {
    server.use(
      http.get(`${BASE}/api/v1/me`, () => HttpResponse.json(problem(401, 'unauthorized'))),
      list([])
    );
    render(ReadMarks, { editionId: 'ED1' });
    await waitFor(() => expect(document.querySelector('section')).toBeNull());
  });
});
