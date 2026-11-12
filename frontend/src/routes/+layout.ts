// Pure SPA served by the backend (ADR-0013): no server rendering, no prerendering.
import { rememberAppearance, sanitize, type Appearance } from '#lib/themes/index.ts';

export const ssr = false;
export const prerender = false;

type Fetch = typeof fetch;

async function read(fetcher: Fetch, path: string): Promise<Record<string, unknown> | null> {
  try {
    const response = await fetcher(path);
    return response.ok ? ((await response.json()) as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** What every screen needs: the address of the source (offered to anyone, ENF-LEG-012) and, for
 * someone who is signed in, the appearance chosen on the server (EF-UX-012). A failure of either
 * never fails the screen. */
export async function load({ fetch: fetcher }: { fetch: Fetch }) {
  const [source, preferences] = await Promise.all([
    read(fetcher, '/api/v1/source'),
    read(fetcher, '/api/v1/me/preferences')
  ]);
  const sourceUrl = typeof source?.source_url === 'string' ? source.source_url : null;
  let appearance: Appearance | null = null;
  if (preferences) {
    appearance = sanitize({ theme: preferences.theme, colorScheme: preferences.color_scheme });
    if (typeof localStorage !== 'undefined') rememberAppearance(localStorage, appearance);
  }
  return { sourceUrl, appearance };
}
