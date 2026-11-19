// The registry of reader engines (ADR-0055, "Port ReaderEngine", D7) and the browsers the EPUB
// engine needs: the inheritance of the CSP by a `blob:` and `'strict-dynamic'`.
import { describe, expect, it, vi } from 'vitest';
import { detectBrowser, supportsEpub } from './browser.ts';
import type { ReaderEngine } from './engine.ts';
import { EPUB_TYPE, EngineRefusedError, REQUIRED_GUARDS, createRegistry, defaultRegistry } from './registry.ts';

const UA = {
  chrome126: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
  chrome109: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36',
  chrome110: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
  edge: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0',
  firefox115: 'Mozilla/5.0 (X11; Linux x86_64; rv:115.0) Gecko/20100101 Firefox/115.0',
  firefox114: 'Mozilla/5.0 (X11; Linux x86_64; rv:114.0) Gecko/20100101 Firefox/114.0',
  safari164: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15',
  safari163: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.3 Safari/605.1.15',
  ios164: 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Mobile/15E148 Safari/604.1',
  ios163: 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.3 Mobile/15E148 Safari/604.1',
  chromeIos163: 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0.0.0 Mobile/15E148 Safari/604.1',
  chromeIos17: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0.0.0 Mobile/15E148 Safari/604.1',
  jsdom: 'Mozilla/5.0 (linux) AppleWebKit/537.36 (KHTML, like Gecko) jsdom/31.0.0'
};

const fake = (mediaTypes: string[], guards: Record<string, boolean> = {}) => ({
  mediaTypes,
  guards,
  load: vi.fn(async () => ({ mediaTypes, guards }) as unknown as ReaderEngine)
});

describe('the browser', () => {
  // req: EF-LEC-003
  it('test_registry_refuses_epub_engine_below_min_browser: the minimums of ADR-0055', () => {
    const ok = (ua: string) => supportsEpub(detectBrowser({ userAgent: ua }));
    expect(ok(UA.chrome126)).toBe(true);
    expect(ok(UA.chrome110)).toBe(true);
    expect(ok(UA.edge)).toBe(true);
    expect(ok(UA.firefox115)).toBe(true);
    expect(ok(UA.safari164)).toBe(true);
    expect(ok(UA.ios164)).toBe(true);
    expect(ok(UA.chromeIos17)).toBe(true);
    expect(ok(UA.chrome109)).toBe(false);
    expect(ok(UA.firefox114)).toBe(false);
    expect(ok(UA.safari163)).toBe(false);
    // C4: the minor is a whole number, `16.10` comes after `16.4` (not a tenth, which would be 16.1)
    const safari1610 = UA.safari164.replace('Version/16.4', 'Version/16.10');
    const ios1610 = UA.ios164.replace('OS 16_4', 'OS 16_10').replace('Version/16.4', 'Version/16.10');
    expect(detectBrowser({ userAgent: safari1610 })).toEqual({ family: 'safari', version: 16, minor: 10 });
    expect(ok(safari1610)).toBe(true);
    expect(ok(ios1610)).toBe(true);
    expect(supportsEpub({ family: 'safari', version: 15, minor: 9 })).toBe(false);
    expect(supportsEpub({ family: 'safari', version: 17, minor: 0 })).toBe(true);
    expect(ok(UA.ios163)).toBe(false);
    expect(ok(UA.chromeIos163)).toBe(false); // every browser of iOS is the WebKit of the system
  });

  // req: EF-LEC-003
  it('refuses when in doubt: unknown, empty, or contradictory', () => {
    expect(supportsEpub(detectBrowser({ userAgent: UA.jsdom }))).toBe(false);
    expect(supportsEpub(detectBrowser({ userAgent: '' }))).toBe(false);
    expect(supportsEpub(null)).toBe(false);
    expect(supportsEpub({ family: 'chromium', version: Number.NaN })).toBe(false);
    const hints = { brands: [{ brand: 'Chromium', version: '100' }, { brand: 'Not.A/Brand', version: '99' }] };
    expect(supportsEpub(detectBrowser({ userAgent: UA.chrome126, userAgentData: hints }))).toBe(false);
  });

  // req: EF-LEC-003
  it('reads the Client Hints first when they exist', () => {
    const hints = { brands: [{ brand: 'Not.A/Brand', version: '99' }, { brand: 'Chromium', version: '126' }, { brand: 'Google Chrome', version: '126' }] };
    expect(detectBrowser({ userAgent: UA.chrome126, userAgentData: hints })).toEqual({ family: 'chromium', version: 126 });
    expect(detectBrowser({ userAgent: 'x', userAgentData: hints })).toEqual({ family: 'chromium', version: 126 });
  });
});

describe('the registry', () => {
  // req: EF-LEC-003
  it('hands out the engine of a type, and builds nothing it was not asked for', async () => {
    const images = fake(['image/png']);
    const epub = fake([EPUB_TYPE], { contentFilter: true });
    const registry = createRegistry({ descriptors: [images, epub], browser: { family: 'chromium', version: 126 } });
    await registry.engineFor('image/png');
    expect(images.load).toHaveBeenCalledTimes(1);
    expect(epub.load).not.toHaveBeenCalled();
  });

  // req: EF-LEC-003
  it('refuses the EPUB engine below the minimum browser, with a typed reason, without loading it', async () => {
    const epub = fake([EPUB_TYPE], { contentFilter: true });
    for (const browser of [{ family: 'safari' as const, version: 16, minor: 3 }, { family: 'firefox' as const, version: 114 }, null]) {
      const registry = createRegistry({ descriptors: [epub], browser });
      await expect(registry.engineFor(EPUB_TYPE)).rejects.toMatchObject({ reason: 'browser' });
      await expect(registry.engineFor(EPUB_TYPE)).rejects.toBeInstanceOf(EngineRefusedError);
    }
    expect(epub.load).not.toHaveBeenCalled();
  });

  // req: EF-LEC-003
  it('does not ask a browser check of the engine of images (no iframe)', async () => {
    const registry = createRegistry({ descriptors: [fake(['image/png'])], browser: null });
    await expect(registry.engineFor('image/png')).resolves.toBeDefined();
  });

  // req: EF-LEC-003
  it('refuses an EPUB engine that does not declare the content filter', () => {
    expect(() => createRegistry({ descriptors: [fake([EPUB_TYPE], {})], browser: null })).toThrow(/contentFilter/);
    expect(REQUIRED_GUARDS[EPUB_TYPE]).toEqual({ contentFilter: true });
  });

  // req: EF-LEC-003
  it('answers a type nobody reads by a typed refusal', async () => {
    const registry = createRegistry({ descriptors: [fake(['image/png'])], browser: null });
    await expect(registry.engineFor('application/x-mobipocket-ebook')).rejects.toMatchObject({ reason: 'unsupported' });
  });
});

describe('no engine for PDF in M5 (D7)', () => {
  // req: ENF-SEC-004
  it('test_pdfjs_is_eval_supported_false: the registry of the app registers no PDF engine', () => {
    const registry = defaultRegistry({ browser: { family: 'chromium', version: 126 }, renew: async () => null });
    expect(registry.mediaTypes).not.toContain('application/pdf');
    expect(registry.mediaTypes).toContain(EPUB_TYPE);
    expect(registry.mediaTypes).toContain('image/jpeg');
  });

  // req: ENF-SEC-004
  it('a PDF engine that does not declare `isEvalSupported: false` fails the construction of the registry', () => {
    expect(REQUIRED_GUARDS['application/pdf']).toEqual({ isEvalSupported: false });
    expect(() => createRegistry({ descriptors: [fake(['application/pdf'])], browser: null })).toThrow(/isEvalSupported/);
    expect(() => createRegistry({ descriptors: [fake(['application/pdf'], { isEvalSupported: true })], browser: null })).toThrow(/isEvalSupported/);
    expect(() => createRegistry({ descriptors: [fake(['application/pdf'], { isEvalSupported: false })], browser: null })).not.toThrow();
  });
});
