import { describe, expect, it, vi } from 'vitest';
import { STORAGE_KEY } from '#lib/themes/index.ts';
import { load } from './+layout.ts';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

function fetcher(routes: Record<string, () => Response>) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    const route = routes[path];
    if (!route) throw new Error(`unexpected ${path}`);
    return route();
  });
}

describe('root layout load', () => {
  // req: ENF-LEG-012
  it('reads the source address, signed in or not', async () => {
    const fetch = fetcher({
      '/api/v1/source': () => json({ source_url: 'https://example.org/src' }),
      '/api/v1/me/preferences': () => json({ title: 'x', status: 401 }, 401)
    });
    const data = await load({ fetch } as never);
    expect(data).toMatchObject({ sourceUrl: 'https://example.org/src', appearance: null });
  });

  // req: ENF-LEG-012
  it('does not fail the screen when the address cannot be read', async () => {
    const fetch = fetcher({
      '/api/v1/source': () => json({}, 500),
      '/api/v1/me/preferences': () => json({}, 500)
    });
    expect(await load({ fetch } as never)).toMatchObject({ sourceUrl: null, appearance: null });
  });

  // req: EF-UX-012
  it('reads the stored preference of a signed-in user and remembers it for the next boot', async () => {
    localStorage.clear();
    const fetch = fetcher({
      '/api/v1/source': () => json({ source_url: 'https://example.org/src' }),
      '/api/v1/me/preferences': () =>
        json({ theme: 'bibliotheque', chosen_theme: null, color_scheme: 'dark' })
    });
    const data = await load({ fetch } as never);
    expect(data.appearance).toEqual({ theme: 'bibliotheque', colorScheme: 'dark' });
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toEqual({
      theme: 'bibliotheque',
      colorScheme: 'dark'
    });
  });

  // req: EF-UX-012, ENF-SEC-004
  it('ignores a theme the client does not know', async () => {
    localStorage.clear();
    const fetch = fetcher({
      '/api/v1/source': () => json({ source_url: 'https://example.org/src' }),
      '/api/v1/me/preferences': () =>
        json({ theme: '"><script>', chosen_theme: null, color_scheme: 'neon' })
    });
    const data = await load({ fetch } as never);
    expect(data.appearance).toEqual({ theme: 'bibliotheque', colorScheme: 'auto' });
  });
});
