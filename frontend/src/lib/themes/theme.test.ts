import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { beforeEach, describe, expect, it } from 'vitest';
import { bootScript } from '../../../scripts/csp.ts';
import {
  COLOR_SCHEMES,
  DEFAULT_THEME,
  STORAGE_KEY,
  THEMES,
  applyAppearance,
  loadAppearance,
  rememberAppearance
} from './index.ts';
import { VARIANT_POINTS, variantProperty } from './variants.ts';

const here = dirname(fileURLToPath(import.meta.url));
const appHtml = readFileSync(join(here, '..', '..', 'app.html'), 'utf8');

/** Run the boot script of `app.html` the way the browser does: once, before anything renders. */
function boot(stored: string | null) {
  document.documentElement.removeAttribute('data-theme');
  document.documentElement.removeAttribute('data-color-scheme');
  if (stored !== null) localStorage.setItem(STORAGE_KEY, stored);
  else localStorage.removeItem(STORAGE_KEY);
  new Function(bootScript(appHtml))();
  const root = document.documentElement;
  return { theme: root.getAttribute('data-theme'), scheme: root.getAttribute('data-color-scheme') };
}

describe('theme mechanism', () => {
  beforeEach(() => localStorage.clear());

  // req: EF-UX-012
  it('ships theme A as the only theme and as the default', () => {
    expect(THEMES).toEqual(['bibliotheque']);
    expect(DEFAULT_THEME).toBe('bibliotheque');
    expect(COLOR_SCHEMES).toEqual(['auto', 'light', 'dark']);
    const dirs = readdirSync(join(here), { withFileTypes: true }).filter((e) => e.isDirectory());
    expect(dirs.map((e) => e.name)).toEqual([...THEMES]);
  });

  // req: EF-UX-012
  it('the boot script sets the attributes from the remembered appearance, before any render', () => {
    expect(document.body.children.length).toBe(0);
    const stored = JSON.stringify({ theme: 'bibliotheque', colorScheme: 'dark' });
    expect(boot(stored)).toEqual({ theme: 'bibliotheque', scheme: 'dark' });
    expect(boot(JSON.stringify({ theme: 'bibliotheque', colorScheme: 'light' }))).toEqual({
      theme: 'bibliotheque',
      scheme: 'light'
    });
  });

  // req: EF-UX-012
  it('the boot script leaves the system scheme alone for "auto" or nothing stored', () => {
    expect(boot(null).scheme).toBeNull();
    expect(boot(JSON.stringify({ theme: 'bibliotheque', colorScheme: 'auto' })).scheme).toBeNull();
  });

  // req: EF-UX-012, ENF-SEC-004
  it.each([
    '{"theme":"<script>alert(1)</script>","colorScheme":"dark"}',
    '{"theme":"console","colorScheme":"neon"}',
    '{"theme":"bibliotheque\\" onload=\\"x","colorScheme":"dark\\"]{}"}',
    '{"theme":{"toString":1},"colorScheme":["dark"]}',
    '{"theme":"__proto__","colorScheme":"constructor"}',
    'not json',
    '"dark"',
    'null',
    ''
  ])('the boot script ignores a tampered storage value: %s', (stored) => {
    const root = boot(stored);
    expect(root.theme).toBe('bibliotheque'); // the markup default, never what was stored
    expect(['dark', 'light', null]).toContain(root.scheme);
    expect(document.documentElement.outerHTML).not.toContain('<script>alert');
  });

  // req: EF-UX-012
  it('the boot script does not throw when the storage is unavailable', () => {
    const real = Object.getOwnPropertyDescriptor(window, 'localStorage')!;
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() {
        throw new Error('blocked');
      }
    });
    try {
      expect(() => new Function(bootScript(appHtml))()).not.toThrow();
    } finally {
      Object.defineProperty(window, 'localStorage', real);
    }
  });

  // req: EF-UX-012
  it('the markup default and the boot script agree with the list of themes', () => {
    expect(appHtml).toMatch(new RegExp(`<html[^>]*data-theme="${DEFAULT_THEME}"`));
    for (const theme of THEMES) expect(bootScript(appHtml)).toContain(`'${theme}'`);
    expect(bootScript(appHtml)).not.toMatch(/console|galerie|calme/);
  });

  // req: EF-UX-012
  it('applies and remembers an appearance, and refuses what is not on the list', () => {
    const root = document.documentElement;
    applyAppearance(root, { theme: 'bibliotheque', colorScheme: 'dark' });
    expect(root.dataset.colorScheme).toBe('dark');
    rememberAppearance(localStorage, { theme: 'bibliotheque', colorScheme: 'dark' });
    expect(loadAppearance(localStorage)).toEqual({ theme: 'bibliotheque', colorScheme: 'dark' });
    applyAppearance(root, { theme: 'bibliotheque', colorScheme: 'auto' });
    expect(root.hasAttribute('data-color-scheme')).toBe(false);
    applyAppearance(root, { theme: 'x"y' as never, colorScheme: 'neon' as never });
    expect(root.dataset.theme).toBe('bibliotheque');
    expect(root.hasAttribute('data-color-scheme')).toBe(false);
    localStorage.setItem(STORAGE_KEY, '{"theme":"nope"}');
    expect(loadAppearance(localStorage)).toEqual({ theme: DEFAULT_THEME, colorScheme: 'auto' });
  });
});

describe('theme variants', () => {
  // req: EF-UX-012
  it('is a closed list of four points, read as CSS properties', () => {
    expect(Object.keys(VARIANT_POINTS).sort()).toEqual(
      ['cover_layout', 'density', 'nav_width', 'series_header'].sort()
    );
    expect(variantProperty('cover_layout')).toBe('--variant-cover-layout');
    expect(variantProperty('nav_width')).toBe('--variant-nav-width');
  });

  // req: EF-UX-012
  it('every theme declares each point with a value of the list, and nothing else', () => {
    const themes = readdirSync(here, { withFileTypes: true }).filter((e) => e.isDirectory());
    for (const theme of themes) {
      const tokens = JSON.parse(readFileSync(join(here, theme.name, 'tokens.json'), 'utf8'));
      const declared = tokens.shared.variant as Record<string, { $value: string }>;
      expect(Object.keys(declared).sort(), theme.name).toEqual(
        Object.keys(VARIANT_POINTS).map((k) => k.replace('_', '-')).sort()
      );
      for (const [point, values] of Object.entries(VARIANT_POINTS)) {
        expect(values as readonly string[], `${theme.name}/${point}`).toContain(
          declared[point.replace('_', '-')].$value
        );
      }
    }
  });

  // req: EF-UX-012
  it('compiles the variants to custom properties under the theme', () => {
    const css = readFileSync(join(here, 'themes.css'), 'utf8');
    expect(css).toMatch(/:root\[data-theme="bibliotheque"\] \{[^}]*--variant-density:/s);
    expect(css).toContain('--variant-nav-width:');
  });
});
