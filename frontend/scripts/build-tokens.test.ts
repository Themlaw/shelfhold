import { describe, expect, it } from 'vitest';
import { compileTheme } from './build-tokens.ts';
import tokens from '../src/lib/themes/bibliotheque/tokens.json';

describe('design tokens', () => {
  it('compiles tokens to custom properties under :root[data-theme]', () => {
    const css: string = compileTheme('demo', {
      light: { color: { bg: { $type: 'color', $value: '#fff' } } },
      dark: { color: { bg: { $type: 'color', $value: '#000' } } }
    });
    expect(css).toContain(':root[data-theme="demo"] {\n  --color-bg: #fff;');
    expect(css).toContain('prefers-color-scheme: dark');
    expect(css).toContain('[data-color-scheme="dark"]');
  });

  it('ships the "Bibliothèque" theme with a light and a dark palette and no web font', () => {
    expect(tokens.light.color.bg.$value).toBe('#F6F1E7');
    expect(tokens.dark.color.bg.$value).toBe('#1A1714');
    expect(JSON.stringify(tokens)).not.toMatch(/https?:|url\(/);
  });
});
