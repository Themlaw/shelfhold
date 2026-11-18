// The browsers the EPUB engine runs on (ADR-0055, "Navigateurs pris en charge"): it needs a
// `blob:` document that inherits the policy of the page that made it, and `'strict-dynamic'`, and
// a build of zip.js that inflates with `DecompressionStream('deflate-raw')`. When in doubt, the
// engine is refused and the reader proposes Kavita.

export type BrowserFamily = 'chromium' | 'firefox' | 'safari';

export interface Browser {
  family: BrowserFamily;
  version: number;
}

/** Safari and iOS 16.4 (WebKit 245682 and 224745, `DecompressionStream`), Chromium 110, Firefox 115. */
export const MIN_BROWSERS: Readonly<Record<BrowserFamily, number>> = { chromium: 110, firefox: 115, safari: 16.4 };

interface Hints {
  brands?: readonly { brand: string; version: string }[];
}
interface NavigatorLike {
  userAgent?: string;
  userAgentData?: Hints;
}

const major = (value: string | undefined): number => (value === undefined ? Number.NaN : Number.parseFloat(value));

function fromHints(hints: Hints | undefined): Browser | null {
  const brand = hints?.brands?.find((b) => b.brand === 'Chromium');
  if (!brand) return null;
  const version = major(brand.version);
  return Number.isFinite(version) ? { family: 'chromium', version } : null;
}

function fromUserAgent(ua: string): Browser | null {
  // every browser of iOS is the WebKit of the system: its version is the one of iOS
  const ios = /\b(?:iPhone|iPad|iPod)\b.*?\bOS (\d+)[_.](\d+)/.exec(ua);
  if (ios) return { family: 'safari', version: Number.parseFloat(`${ios[1]}.${ios[2]}`) };
  const firefox = /\bFirefox\/(\d+(?:\.\d+)?)/.exec(ua);
  if (firefox) return { family: 'firefox', version: major(firefox[1]) };
  const chromium = /\b(?:Chrome|Chromium)\/(\d+)/.exec(ua);
  if (chromium) return { family: 'chromium', version: major(chromium[1]) };
  const safari = /\bVersion\/(\d+)\.(\d+)[^]*\bSafari\//.exec(ua);
  if (safari) return { family: 'safari', version: Number.parseFloat(`${safari[1]}.${safari[2]}`) };
  return null;
}

/** The browser, from the Client Hints when there are some, else from the user agent; `null` if unknown.
 * Both known and disagreeing: the lower version, since one of the two is wrong. */
export function detectBrowser(nav: NavigatorLike): Browser | null {
  const hinted = fromHints(nav.userAgentData);
  const agent = fromUserAgent(nav.userAgent ?? '');
  if (hinted && agent && agent.family === 'chromium') return hinted.version <= agent.version ? hinted : agent;
  return hinted ?? agent;
}

export function supportsEpub(browser: Browser | null): boolean {
  if (browser === null || !Number.isFinite(browser.version)) return false;
  return browser.version >= MIN_BROWSERS[browser.family];
}
