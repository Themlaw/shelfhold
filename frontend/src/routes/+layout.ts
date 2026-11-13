// Pure SPA served by the backend (ADR-0013): no server rendering, no prerendering.
import { rememberAppearance, sanitize, type Appearance } from '#lib/themes/index.ts';
import type { Language } from '#lib/i18n/index.ts';

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

/** The language of the interface is a preference of the profile (ENF-I18N-001); nothing set (nobody
 * signed in, or an unknown value): the browser languages decide (the `Accept-Language`). */
function language(preferences: Record<string, unknown> | null): Language | null {
  const value = preferences?.language;
  return value === 'fr' || value === 'en' ? value : null;
}

/** What every screen needs: the address of the source (offered to anyone, ENF-LEG-012) and, for
 * someone who is signed in, the appearance chosen on the server (EF-UX-012) and the navigation of their role (EF-UX-005). A failure of either
 * never fails the screen. */
export async function load({ fetch: fetcher }: { fetch: Fetch }) {
  const [source, preferences, me] = await Promise.all([
    read(fetcher, '/api/v1/source'),
    read(fetcher, '/api/v1/me/preferences'),
    read(fetcher, '/api/v1/me')
  ]);
  const sourceUrl = typeof source?.source_url === 'string' ? source.source_url : null;
  let appearance: Appearance | null = null;
  if (preferences) {
    appearance = sanitize({ theme: preferences.theme, colorScheme: preferences.color_scheme });
    if (typeof localStorage !== 'undefined') rememberAppearance(localStorage, appearance);
  }
  // The tabs the server offers to the signed-in role (EF-UX-005); nobody signed in: none.
  const nav = Array.isArray(me?.nav) ? me.nav.filter((id): id is string => typeof id === 'string') : [];
  return { sourceUrl, appearance, nav, language: language(preferences) };
}
