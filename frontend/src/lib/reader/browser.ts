// The browsers the EPUB engine runs on (ADR-0055, "supported browsers"): it needs a
// `blob:` document that inherits the policy of the page that made it, and `'strict-dynamic'`, and
// a build of zip.js that inflates with `DecompressionStream('deflate-raw')`. When in doubt, the
// engine is refused and the reader proposes Kavita.

export type BrowserFamily = 'chromium' | 'firefox' | 'safari';

export interface Browser {
  family: BrowserFamily;
  /** The major version: Chromium 126, Firefox 115, Safari 16. */
  version: number;
  /** The minor version, read as a whole number (`16.10` is after `16.4`); Safari and iOS only. */
  minor?: number;
}

/** Safari and iOS 16.4 (WebKit 245682 and 224745, `DecompressionStream`), Chromium 110, Firefox 115. */
export const MIN_BROWSERS: Readonly<Record<BrowserFamily, readonly [number, number]>> = {
  chromium: [110, 0],
  firefox: [115, 0],
  safari: [16, 4]
};

interface Hints {
  brands?: readonly { brand: string; version: string }[];
}
interface NavigatorLike {
  userAgent?: string;
  userAgentData?: Hints;
}

/** A component of a version, as a whole number: `10` is ten, not a tenth. */
const whole = (value: string | undefined): number => (value === undefined ? Number.NaN : Number.parseInt(value, 10));
const major = (value: string | undefined): number => whole(value?.split('.')[0]);

function fromHints(hints: Hints | undefined): Browser | null {
  const brand = hints?.brands?.find((b) => b.brand === 'Chromium');
  if (!brand) return null;
  const version = major(brand.version);
  return Number.isFinite(version) ? { family: 'chromium', version } : null;
}

function fromUserAgent(ua: string): Browser | null {
  // every browser of iOS is the WebKit of the system: its version is the one of iOS
  const ios = /\b(?:iPhone|iPad|iPod)\b.*?\bOS (\d+)[_.](\d+)/.exec(ua);
  if (ios) return { family: 'safari', version: whole(ios[1]), minor: whole(ios[2]) };
  const firefox = /\bFirefox\/(\d+)/.exec(ua);
  if (firefox) return { family: 'firefox', version: major(firefox[1]) };
  const chromium = /\b(?:Chrome|Chromium)\/(\d+)/.exec(ua);
  if (chromium) return { family: 'chromium', version: major(chromium[1]) };
  const safari = /\bVersion\/(\d+)\.(\d+)[^]*\bSafari\//.exec(ua);
  if (safari) return { family: 'safari', version: whole(safari[1]), minor: whole(safari[2]) };
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
  const [major, minor] = MIN_BROWSERS[browser.family];
  const found = browser.minor ?? 0;
  if (!Number.isFinite(found)) return false;
  return browser.version > major || (browser.version === major && found >= minor);
}
