import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Chapters from './Chapters.svelte';

const BASE = 'http://shelfhold.test';
const ENDPOINT = `${BASE}/api/v1/editions/ED1/units`;
const server = setupServer();

const unit = (n: number, over: Record<string, unknown> = {}) => ({
  id: `U${n}`,
  number: String(n),
  title: `Chapitre d’essai ${n}`,
  kind: 'chapter',
  track: 'main',
  volume_id: null,
  added_at: '2026-11-10T09:30:00Z',
  read_state: 'unread',
  readable: true,
  ...over
});
const page = (items: unknown[], total = items.length, offset = 0) =>
  HttpResponse.json({ edition_id: 'ED1', items, total, limit: 50, offset });

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Chapters', () => {
  // req: EF-BIB-005
  it('lists the chapters in the order the server gives, with the read state and a link to the reader', async () => {
    server.use(
      http.get(ENDPOINT, () =>
        page([
          unit(1, { read_state: 'read' }),
          unit(2, { read_state: 'in_progress' }),
          unit(3),
          unit(4, { readable: false, title: null })
        ])
      )
    );
    render(Chapters, { editionId: 'ED1' });
    const list = await screen.findByRole('list', { name: 'Chapitres' });
    const rows = within(list).getAllByRole('listitem');
    expect(rows).toHaveLength(4);
    expect(within(rows[0]!).getByRole('link', { name: 'Chapitre d’essai 1' })).toHaveAttribute('href', '/lire/unit/U1');
    expect(within(rows[0]!).getByText('Lu')).toBeInTheDocument();
    expect(within(rows[1]!).getByText('En cours')).toBeInTheDocument();
    // no state is made up for an unread chapter
    expect(within(rows[2]!).queryByText(/Lu|En cours/)).toBeNull();
    // a chapter the reader cannot open is not a link; no title: the number says which one
    expect(within(rows[3]!).queryByRole('link')).toBeNull();
    expect(within(rows[3]!).getByText('Chapitre 4')).toBeInTheDocument();
  });

  // req: EF-BIB-005
  it('asks for the next page by offset when the list is longer than a page', async () => {
    const offsets: (string | null)[] = [];
    server.use(
      http.get(ENDPOINT, ({ request }) => {
        const offset = new URL(request.url).searchParams.get('offset');
        offsets.push(offset);
        return offset === '50' ? page([unit(51)], 51, 50) : page([unit(1)], 51, 0);
      })
    );
    render(Chapters, { editionId: 'ED1' });
    const user = userEvent.setup();
    await screen.findByText('Chapitre d’essai 1');
    await user.click(screen.getByRole('button', { name: /Suivant|2/ }));
    expect(await screen.findByText('Chapitre d’essai 51')).toBeInTheDocument();
    expect(offsets).toEqual(['0', '50']);
  });

  // req: EF-BIB-005
  it('says so when the edition has no chapter yet', async () => {
    server.use(http.get(ENDPOINT, () => page([])));
    render(Chapters, { editionId: 'ED1' });
    expect(await screen.findByText('Aucun chapitre n’est encore disponible pour cette édition.')).toBeInTheDocument();
  });

  // req: EF-BIB-005
  it('shows the loading state, then an error with a way to try again', async () => {
    let fail = true;
    server.use(
      http.get(ENDPOINT, () => (fail ? HttpResponse.json({ title: 'x', status: 500, code: 'internal' }, { status: 500 }) : page([unit(1)])))
    );
    render(Chapters, { editionId: 'ED1' });
    expect(screen.getByRole('status')).toHaveTextContent('Chargement des chapitres…');
    const user = userEvent.setup();
    expect(await screen.findByText('Les chapitres n’ont pas pu être lus')).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole('button', { name: 'Réessayer' }));
    expect(await screen.findByText('Chapitre d’essai 1')).toBeInTheDocument();
  });
});

