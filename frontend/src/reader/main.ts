// The entry of the reader (`/lire/<carrier_type>/<carrier_id>[?scope=excerpt]`, ADR-0055): a page of
// its own, outside the SvelteKit router, reached by a full navigation, served under its own policy.
// The theme and the language are the ones of the SPA, read the same way (nothing here is a script of
// the page: this module is imported by the one script the policy hashes).
import { mount } from 'svelte';
import { detectLanguage, setUiLanguage } from '#lib/i18n/index.ts';
import { applyAppearance, loadAppearance } from '#lib/themes/index.ts';
import '#lib/themes/base.css';
import '#lib/themes/themes.css';
import '#lib/themes/foundation.css'; // spacing, type scale, fonts, motion: the tokens the components read
import '../routes/global.css';
import { parseLocation } from './location.ts';
import Reader from './Reader.svelte';

try {
  applyAppearance(document.documentElement, loadAppearance(localStorage));
} catch {
  /* storage is closed: the default theme */
}
setUiLanguage(detectLanguage(navigator.languages));

const target = document.getElementById('reader');
const opened = parseLocation(location.pathname, location.search);
if (target) mount(Reader, { target, props: opened ?? { carrierType: '', carrierId: '', scope: null } });
