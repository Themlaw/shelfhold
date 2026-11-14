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

  // req: EF-UX-005
  it('reads the navigation of the signed-in role from /api/v1/me, and none for nobody', async () => {
    const signedIn = fetcher({
      '/api/v1/source': () => json({ source_url: 'https://example.org/src' }),
      '/api/v1/me/preferences': () => json({}, 401),
      '/api/v1/me': () => json({ role: 'member', nav: ['home', 'search', 7, 'requests'] })
    });
    expect((await load({ fetch: signedIn } as never)).nav).toEqual(['home', 'search', 'requests']);
    const nobody = fetcher({
      '/api/v1/source': () => json({}, 200),
      '/api/v1/me/preferences': () => json({}, 401),
      '/api/v1/me': () => json({ status: 401 }, 401)
    });
    expect((await load({ fetch: nobody } as never)).nav).toEqual([]);
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

  // req: ENF-UX-001, EF-UX-001
  it('sends a new instance, from any screen, to the account screen of the wizard', async () => {
    const fetch = fetcher({
      '/api/v1/setup/status': () => json({ needs_admin: true, token_configured: true })
    });
    for (const path of ['/', '/bibliotheque', '/assistant']) {
      await expect(load({ fetch, url: new URL(`http://x${path}`) } as never)).rejects.toMatchObject({
        status: 307,
        location: '/assistant/compte'
      });
    }
  });

  // req: ENF-UX-001
  it('leaves the wizard screens alone on a new instance and the rest alone on an instance with its admin', async () => {
    const fresh = fetcher({
      '/api/v1/setup/status': () => json({ needs_admin: true, token_configured: true })
    });
    await expect(
      load({ fetch: fresh, url: new URL('http://x/assistant/compte') } as never)
    ).resolves.toBeDefined();
    const settled = fetcher({
      '/api/v1/setup/status': () => json({ needs_admin: false, token_configured: true })
    });
    await expect(load({ fetch: settled, url: new URL('http://x/') } as never)).resolves.toBeDefined();
  });

  // req: ENF-UX-001
  it('does not ask for the status of the instance once somebody is signed in', async () => {
    const fetch = fetcher({
      '/api/v1/source': () => json({}, 200),
      '/api/v1/me/preferences': () => json({}, 200),
      '/api/v1/me': () => json({ role: 'admin', nav: [] }),
      '/api/v1/setup/status': () => json({ needs_admin: true })
    });
    await expect(load({ fetch, url: new URL('http://x/') } as never)).resolves.toBeDefined();
    expect(fetch.mock.calls.map((c) => String(c[0]))).not.toContain('/api/v1/setup/status');
  });

  // req: ENF-UX-001
  it('reads the address untracked, so that a navigation does not run the reads again', async () => {
    const fetch = fetcher({ '/api/v1/setup/status': () => json({ needs_admin: false }) });
    const untrack = vi.fn(<T,>(fn: () => T) => fn());
    await load({ fetch, url: new URL('http://x/bibliotheque'), untrack } as never);
    expect(untrack).toHaveBeenCalled();
  });
});
