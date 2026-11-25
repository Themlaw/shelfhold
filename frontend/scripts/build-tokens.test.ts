import { describe, expect, it } from 'vitest';
import { compileTheme } from './build-tokens.ts';
import tokens from '../src/lib/themes/bibliotheque/tokens.json';

describe('design tokens', () => {
  // req: EF-UX-012
  it('compiles tokens to custom properties under :root[data-theme]', () => {
    const css: string = compileTheme('demo', {
      light: { color: { bg: { $type: 'color', $value: '#fff' } } },
      dark: { color: { bg: { $type: 'color', $value: '#000' } } }
    });
    expect(css).toContain(':root[data-theme="demo"] {\n  --color-bg: #fff;');
    expect(css).toContain('prefers-color-scheme: dark');
    expect(css).toContain('[data-color-scheme="dark"]');
  });

  // req: EF-UX-012
  it('ships the "Bibliothèque" theme with a light and a dark palette and no remote font', () => {
    expect(tokens.light.color.bg.$value).toBe('#F1ECE1');
    expect(tokens.dark.color.bg.$value).toBe('#191714');
    expect(tokens.shared.font.text.$value).toMatch(/^Alegreya,/);
    expect(JSON.stringify(tokens)).not.toMatch(/https?:|url\(/);
  });

  // req: EF-UX-012
  it('no colour of a theme is one the CSS minifier shortens (#000000 is shipped as #000)', () => {
    // the image journey compares the colours the browser applied with tokens.json, as written
    const themes = import.meta.glob('../src/lib/themes/*/tokens.json', { eager: true, import: 'default' });
    const shortenable = /#([0-9a-f])\1([0-9a-f])\2([0-9a-f])\3\b/i;
    for (const [path, theme] of Object.entries(themes)) {
      expect(JSON.stringify(theme), path).not.toMatch(shortenable);
    }
  });
});
