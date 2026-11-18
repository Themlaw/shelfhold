// The entry of the reader (`/lire/<carrier_type>/<carrier_id>[?scope=excerpt]`, ADR-0055): a page of
// its own, outside the SvelteKit router, reached by a full navigation, served under its own policy.
// The theme and the language are the ones of the SPA, read the same way (nothing here is a script of
// the page: this module is imported by the one script the policy hashes).
import { mount } from 'svelte';
import { detectLanguage, setUiLanguage } from '#lib/i18n/index.ts';
import { applyAppearance, loadAppearance } from '#lib/themes/index.ts';
import '#lib/themes/base.css';
import '#lib/themes/themes.css';
import '../routes/global.css';
import Reader from './Reader.svelte';

/** `/lire/unit/U1?scope=excerpt` → what the reader opens; anything else is nothing to open. */
export function parseLocation(pathname: string, search: string): { carrierType: string; carrierId: string; scope: 'excerpt' | null } | null {
  const match = /^\/lire\/([A-Za-z_]{1,32})\/([A-Za-z0-9_-]{1,64})\/?$/.exec(pathname);
  if (!match) return null;
  return {
    carrierType: match[1]!,
    carrierId: match[2]!,
    scope: new URLSearchParams(search).get('scope') === 'excerpt' ? 'excerpt' : null
  };
}

try {
  applyAppearance(document.documentElement, loadAppearance(localStorage));
} catch {
  /* storage is closed: the default theme */
}
setUiLanguage(detectLanguage(navigator.languages));

const target = document.getElementById('reader');
const opened = parseLocation(location.pathname, location.search);
if (target) mount(Reader, { target, props: opened ?? { carrierType: '', carrierId: '', scope: null } });
