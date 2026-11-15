import { render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import { setUiLanguage, t } from '#lib/i18n/index.ts';
import SupportAuthor from './SupportAuthor.svelte';

const BASE = 'http://shelfhold.test';
const API = `${BASE}/api/v1/editions/ED1`;
const server = setupServer();

const link = (url: string, over: Record<string, unknown> = {}) => ({
  url,
  kind: 'official_read',
  lang: 'fr',
  provider: 'anilist',
  fetched_at: '2026-11-15T09:00:00Z',
  ...over
});
const answer = (links: unknown[], pause: string[] = []) =>
  http.get(`${API}/support-author`, () =>
    HttpResponse.json({ edition_id: 'ED1', links, pause_suggestions: pause })
  );

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

describe('Support the author', () => {
  // req: EF-BIB-032
  it('test_support_author_official_links_with_provenance', async () => {
    server.use(answer([link('https://publisher.example.org/read')]));
    render(SupportAuthor, { editionId: 'ED1' });
    const anchor = await screen.findByRole('link', { name: /anilist/ });
    expect(anchor).toHaveAttribute('href', 'https://publisher.example.org/read');
    expect(screen.getByText(/Source : anilist, relevé le/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: "Soutenir l'auteur" })).toBeInTheDocument();
  });

  // req: EF-BIB-032
  it('test_official_link_https_only_and_rel_noopener', async () => {
    server.use(
      answer([
        link('https://ok.example.org/a'),
        link('javascript:alert(1)'),
        link('data:text/html,x'),
        link('http://plain.example.org/b'),
        link('not a url'),
        link('//example.org/c')
      ])
    );
    render(SupportAuthor, { editionId: 'ED1' });
    await screen.findByRole('link');
    const anchors = screen.getAllByRole('link');
    expect(anchors).toHaveLength(1); // nothing but plain https is drawn
    expect(anchors[0]).toHaveAttribute('href', 'https://ok.example.org/a');
    expect(anchors[0]).toHaveAttribute('target', '_blank');
    expect(anchors[0]).toHaveAttribute('rel', 'noopener noreferrer');
    expect(document.body.innerHTML).not.toMatch(/javascript:|data:text/);
  });

  // req: EF-BIB-032
  it('test_support_author_shows_each_kind_in_both_languages', async () => {
    const links = [
      link('https://a.example.org/r', { kind: 'official_read', provider: 'anilist' }),
      link('https://b.example.org/b', { kind: 'buy', provider: 'mangaupdates', lang: 'und' }),
      link('https://c.example.org/l', { kind: 'licensed_in', provider: 'anilist', lang: 'en' })
    ];
    server.use(answer(links));
    const view = render(SupportAuthor, { editionId: 'ED1' });
    await screen.findByText('Lire : anilist');
    expect(screen.getByText('Acheter : mangaupdates')).toBeInTheDocument();
    expect(screen.getByText('Édition licenciée : anilist')).toBeInTheDocument();
    expect(screen.getByText('Langue : fr')).toBeInTheDocument();
    expect(screen.queryByText('Langue : und')).not.toBeInTheDocument();
    view.unmount();
    setUiLanguage('en');
    render(SupportAuthor, { editionId: 'ED1' });
    await screen.findByText('Read: anilist');
    expect(screen.getByText('Buy: mangaupdates')).toBeInTheDocument();
    expect(screen.getByText('Licensed edition: anilist')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Support the author' })).toBeInTheDocument();
  });

  // req: EF-BIB-032
  it('test_support_author_draws_nothing_for_an_edition_she_may_not_see', async () => {
    server.use(
      http.get(`${API}/support-author`, () =>
        HttpResponse.json({ type: '/api/v1/problems/not_found', title: 'x', status: 404 }, { status: 404 })
      )
    );
    const view = render(SupportAuthor, { editionId: 'ED1' });
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(view.container.querySelector('section')).toBeNull();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument(); // no error that tells it exists
  });

  // req: EF-BIB-032
  it('test_support_author_draws_nothing_without_links_or_proposal', async () => {
    server.use(answer([]));
    const view = render(SupportAuthor, { editionId: 'ED1' });
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(view.container.querySelector('section')).toBeNull();
  });

  // req: EF-BIB-032
  it('test_official_edition_in_household_language_suggests_pausing_binding', async () => {
    const user = userEvent.setup();
    let calls = 0;
    let suggestions = ['BND1'];
    server.use(
      http.get(`${API}/support-author`, () =>
        HttpResponse.json({ edition_id: 'ED1', links: [], pause_suggestions: suggestions })
      ),
      http.post(`${API}/bindings/BND1/pause`, ({ request }) => {
        calls += 1;
        expect(request.headers.get('X-CSRF-Token')).toBe('tok123');
        suggestions = [];
        return HttpResponse.json({ edition_id: 'ED1', binding_id: 'BND1', paused: true });
      })
    );
    render(SupportAuthor, { editionId: 'ED1' });
    const button = await screen.findByRole('button', { name: t('support.pause.action') });
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(calls).toBe(0); // a proposal: nothing is paused by showing it
    await user.click(button);
    await waitFor(() => expect(calls).toBe(1));
    expect(await screen.findByRole('status')).toHaveTextContent('Suivi communautaire suspendu.');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  // req: EF-BIB-032
  it('test_pausing_refused_shows_the_problem_and_keeps_the_proposal', async () => {
    const user = userEvent.setup();
    server.use(
      answer([], ['BND1']),
      http.post(`${API}/bindings/BND1/pause`, () =>
        HttpResponse.json(
          { type: '/api/v1/problems/not_found', title: 'Introuvable', status: 404, code: 'not_found' },
          { status: 404 }
        )
      )
    );
    render(SupportAuthor, { editionId: 'ED1' });
    await user.click(await screen.findByRole('button', { name: t('support.pause.action') }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: t('support.pause.action') })).toBeInTheDocument();
  });
});
