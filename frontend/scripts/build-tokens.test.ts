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
});
