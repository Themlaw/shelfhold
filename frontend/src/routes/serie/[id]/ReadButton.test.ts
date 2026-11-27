import { render, screen } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import ReadButton from './ReadButton.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();

const publication = (scope: 'full' | 'excerpt') => ({
  carrier_id: 'C1',
  carrier_type: 'content',
  expires_at: '2026-11-18T12:10:00Z',
  kind: 'pages',
  language: 'fr',
  number: '1',
  package: null,
  pages: [{ index: 0, href: '/content/T/p1.jpg', media_type: 'image/jpeg' }],
  reading_direction: 'ltr',
  scope,
  spine: [],
  title: 'Une série'
});
const problem = (status: number, code: string) =>
  HttpResponse.json({ type: `/api/v1/problems/${code}`, title: code, status, code }, { status });
const answer = (reply: () => Response, unit = 'U1') =>
  http.get(`${BASE}/api/reader/unit/${unit}/publication`, reply);

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('the read button of the series sheet', () => {
  // req: EF-LEC-001
  it('offers "Lire" when the kept copy may be read in full', async () => {
    server.use(answer(() => HttpResponse.json(publication('full'))));
    render(ReadButton, { unitId: 'U1' });
    const link = await screen.findByRole('link', { name: 'Lire' });
    expect(link).toHaveAttribute('href', '/lire/unit/U1');
  });

  // req: EF-UX-006
  it('offers "Lire un extrait" when only an extract may be read here', async () => {
    server.use(answer(() => HttpResponse.json(publication('excerpt'))));
    render(ReadButton, { unitId: 'U1' });
    const link = await screen.findByRole('link', { name: 'Lire un extrait' });
    expect(link).toHaveAttribute('href', '/lire/unit/U1?scope=excerpt');
  });

  // req: EF-UX-006
  it.each([
    ['nothing to read', 409, 'no_readable_copy'],
    ['an extract that cannot be cut', 409, 'excerpt_unavailable'],
    ['a book the profile may not see', 404, 'not_found']
  ])('offers nothing for %s', async (_name, status, code) => {
    server.use(answer(() => problem(status, code)));
    render(ReadButton, { unitId: 'U1' });
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(screen.queryByRole('link')).toBeNull();
  });

  // req: EF-UX-006
  it('offers an EPUB like any book, through a full navigation to the reader (its own page, its own policy)', async () => {
    server.use(answer(() => HttpResponse.json({ ...publication('full'), kind: 'epub', pages: [] })));
    render(ReadButton, { unitId: 'U1' });
    const link = await screen.findByRole('link', { name: 'Lire' });
    expect(link).toHaveAttribute('href', '/lire/unit/U1');
    expect(link).toHaveAttribute('data-sveltekit-reload');
  });

  // req: EF-UX-006
  it('follows the Unit when the sheet is kept for another series', async () => {
    server.use(
      http.get(`${BASE}/api/reader/unit/U1/publication`, () => HttpResponse.json(publication('full'))),
      http.get(`${BASE}/api/reader/unit/U2/publication`, () => problem(409, 'no_readable_copy'))
    );
    const { rerender } = render(ReadButton, { unitId: 'U1' });
    await screen.findByRole('link', { name: 'Lire' });
    await rerender({ unitId: 'U2' });
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(screen.queryByRole('link')).toBeNull(); // the label of the other series is not kept
  });

  // req: EF-UX-006
  it('does not ask anything when the edition has no unit', async () => {
    render(ReadButton, { unitId: null });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole('link')).toBeNull();
  });

  // req: EF-UX-006
  it('never calls the verb "Exporter"', async () => {
    server.use(answer(() => HttpResponse.json(publication('excerpt'))));
    const { container } = render(ReadButton, { unitId: 'U1' });
    await screen.findByRole('link');
    expect(container.textContent).not.toMatch(/export/i);
  });

  // req: EF-LEC-001
  it('resumes at the chapter under way when the sheet gives a reading, and starts when it gives none', async () => {
    const progress = {
      unit_id: 'U7', number: '7', unit_title: null, state: 'in_progress' as const,
      percent: 0.4, device: null, last_read_at: '2026-11-20T10:00:00Z', read_count: 6
    };
    server.use(answer(() => HttpResponse.json(publication('full')), 'U7'), answer(() => HttpResponse.json(publication('full'))));
    const first = render(ReadButton, { unitId: 'U1', progress });
    const link = await screen.findByRole('link', { name: 'Reprendre au chapitre 7' });
    expect(link).toHaveAttribute('href', '/lire/unit/U7');
    first.unmount();

    render(ReadButton, { unitId: 'U1', progress: null });
    const start = await screen.findByRole('link', { name: 'Commencer' });
    expect(start).toHaveAttribute('href', '/lire/unit/U1');
  });
});
