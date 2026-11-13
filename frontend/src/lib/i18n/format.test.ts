import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { formatCurrency, formatDate, formatDateTime, formatNumber, formatPercent, typeset } from './format';

const NNBSP = '\u202f';
const NBSP = '\u00a0';
const NOON = new Date(Date.UTC(2026, 10, 3, 14, 5));

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === 'api' ? [] : sources(path);
    return /\.(ts|svelte)$/.test(name) && !name.endsWith('.test.ts') ? [path] : [];
  });
}

describe('dates and numbers (ENF-I18N-003)', () => {
  // req: ENF-I18N-003
  it('test_dates_rendered_with_intl_fr: month names in French, day first', () => {
    expect(formatDate(NOON, { dateStyle: 'long', timeZone: 'UTC' })).toBe('3 novembre 2026');
    expect(formatDate(NOON, { timeZone: 'UTC' })).toBe('03/11/2026');
    expect(formatDate('2026-02-01T00:00:00Z', { dateStyle: 'full', timeZone: 'UTC' })).toBe('dimanche 1 février 2026');
    expect(formatDateTime(NOON, { timeZone: 'UTC' })).toMatch(/^3 nov\. 2026.*14:05$/);
  });

  // req: ENF-I18N-003
  it('uses the narrow no-break space as the thousands separator and a decimal comma', () => {
    expect(formatNumber(1234567.5)).toBe(`1${NNBSP}234${NNBSP}567,5`);
    expect(formatNumber(999)).toBe('999');
    expect(formatPercent(0.5)).toMatch(/^50[\u00a0\u202f]%$/); // a no-break space, whichever width ICU picks
    expect(formatCurrency(1234.5)).toMatch(/^1\u202f234,50[\u00a0\u202f]€$/);
  });

  // req: ENF-I18N-003
  it('typeset puts a narrow no-break space before ; : ! ? and nothing else', () => {
    expect(typeset('Bonjour : monde ; oui ! non ?')).toBe(`Bonjour${NNBSP}: monde${NNBSP}; oui${NNBSP}! non${NNBSP}?`);
    expect(typeset(`déjà${NBSP}: fait`)).toBe(`déjà${NNBSP}: fait`);
    expect(typeset(`déjà${NNBSP}: fait`)).toBe(`déjà${NNBSP}: fait`);
    // A command, an address or a user:password pair keeps its characters.
    expect(typeset('Lancez `a : b`, http://hote:8080 et nom:mot_de_passe')).toBe(
      'Lancez `a : b`, http://hote:8080 et nom:mot_de_passe'
    );
  });

  // req: ENF-I18N-003
  it('no screen formats a date or a number by hand: everything goes through format.ts', () => {
    const offenders = sources('src')
      .filter((path) => !path.endsWith(join('i18n', 'format.ts')))
      .filter((path) =>
        /toLocale(?:Date|Time)?String|Intl\.(?:DateTimeFormat|NumberFormat)|\.toFixed\(|getMonth\(/.test(readFileSync(path, 'utf8'))
      );
    expect(offenders).toEqual([]);
  });
});
