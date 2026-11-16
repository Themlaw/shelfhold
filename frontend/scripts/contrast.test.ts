import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { AA_TEXT, checkAll, contrastRatio, failingPairs, pairsOf, themesDir } from './contrast.ts';

// req-file: ENF-A11Y-001
describe('contrast', () => {
  it('computes the WCAG ratio', () => {
    expect(contrastRatio('#000000', '#FFFFFF')).toBeCloseTo(21, 5);
    expect(contrastRatio('#777777', '#777777')).toBeCloseTo(1, 5);
    expect(contrastRatio('#FFF', '#000')).toBeCloseTo(21, 5);
  });

  // req: ENF-A11Y-001
  it('test_token_contrast_aa', () => {
    const themes = readdirSync(themesDir, { withFileTypes: true }).filter((e) => e.isDirectory());
    expect(themes.length).toBeGreaterThan(0);
    for (const theme of themes) {
      const tokens = JSON.parse(readFileSync(join(themesDir, theme.name, 'tokens.json'), 'utf8'));
      for (const scheme of ['light', 'dark'] as const) {
        expect(pairsOf(tokens[scheme]).length, `${theme.name}/${scheme}`).toBeGreaterThan(8);
      }
      expect(failingPairs(tokens), theme.name).toEqual([]);
    }
  });

  // req: EF-UX-012
  it('test_every_theme_tokens_pass_wcag_aa', () => {
    const themes = readdirSync(themesDir, { withFileTypes: true }).filter((e) => e.isDirectory());
    expect(themes.map((e) => e.name)).toContain('bibliotheque'); // theme A is the one that ships
    for (const theme of themes) {
      const tokens = JSON.parse(readFileSync(join(themesDir, theme.name, 'tokens.json'), 'utf8'));
      expect(tokens.dark, `${theme.name} has a dark scheme`).toBeDefined();
      expect(checkAll(), theme.name).toEqual([]);
      for (const scheme of ['light', 'dark'] as const) {
        const { names } = pairsOf(tokens[scheme]).reduce(
          (acc, p) => ({ names: [...acc.names, p.names] }),
          { names: [] as string[] }
        );
        expect(names, `${theme.name}/${scheme} checks the label of the accent button`).toContain(
          'on-accent/accent'
        );
      }
    }
  });

  it('holds the ochre and the grey of the light theme to AA', () => {
    const tokens = JSON.parse(
      readFileSync(join(themesDir, 'bibliotheque', 'tokens.json'), 'utf8')
    );
    expect(tokens.light.color.accent.$value).toBe('#A3471F');
    expect(tokens.light.color.muted.$value).toBe('#6E6357');
    expect(contrastRatio('#A3471F', tokens.light.color.bg.$value)).toBeGreaterThanOrEqual(AA_TEXT);
    expect(contrastRatio('#6E6357', tokens.light.color.bg.$value)).toBeGreaterThanOrEqual(AA_TEXT);
  });

  it('flags a pair under AA, in either scheme', () => {
    const base = { color: { bg: '#FFFFFF', surface: '#FFFFFF', ink: '#000000', muted: '#000000', accent: '#000000', 'on-accent': '#FFFFFF', ok: '#000000', warn: '#000000', err: '#000000' } };
    const wrap = (c: Record<string, string>) => ({
      color: Object.fromEntries(Object.entries(c).map(([k, v]) => [k, { $value: v }]))
    });
    const good = wrap(base.color);
    expect(failingPairs({ light: good, dark: good })).toEqual([]);
    const bad = wrap({ ...base.color, muted: '#BBBBBB' });
    expect(failingPairs({ light: good, dark: bad }).map((f) => f.scheme)).toEqual(['dark', 'dark']);
    expect(failingPairs({ light: bad, dark: good }).map((f) => f.scheme)).toEqual(['light', 'light']);
  });
});
