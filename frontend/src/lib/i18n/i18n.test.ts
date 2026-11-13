import { IntlMessageFormat } from 'intl-messageformat';
import { describe, expect, it } from 'vitest';
import fr from './fr.json';
import en from './en.json';
import { detectLanguage, setUiLanguage, t } from './index';

const NNBSP = '\u202f';
const NBSP = '\u00a0';
const BEFORE_PUNCTUATION = new RegExp(`[ ${NBSP}](?=[;:!?])`);


type Node = { type: number; value?: string; options?: Record<string, { value: Node[] }> };

/** Every argument of a message, with a value of the right kind (plural and number want a number). */
function values(nodes: Node[], found: Record<string, string | number> = {}): Record<string, string | number> {
  for (const node of nodes) {
    if (node.type === 1) found[node.value ?? ''] = 'x';
    if (node.type === 2 || node.type === 6) found[node.value ?? ''] = 2;
    for (const option of Object.values(node.options ?? {})) values(option.value, found);
  }
  return found;
}

function render(key: keyof typeof fr): string {
  const ast = new IntlMessageFormat(fr[key], 'fr').getAst() as Node[];
  return t(key, values(ast));
}

describe('i18n', () => {
  // req: ENF-I18N-001
  it('formats simple messages', () => {
    expect(t('health.ok')).toBe('Le serveur répond.');
  });

  // req: ENF-I18N-001
  it('applies ICU plural rules in French (0 and 1 are singular)', () => {
    expect(t('health.migrating', { done: 0, total: 5 })).toContain('aucune étape terminée');
    expect(t('health.migrating', { done: 1, total: 5 })).toContain('1 étape terminée');
    expect(t('health.migrating', { done: 3, total: 5 })).toContain('3 étapes terminées');
  });

  // req: ENF-I18N-003
  it('test_icu_plurals_and_narrow_nbsp: thousands are grouped with a narrow no-break space', () => {
    expect(t('health.migrating', { done: 1500, total: 12000 })).toBe(
      `Migration en cours${NNBSP}: 1${NNBSP}500 étapes terminées sur 12${NNBSP}000.`
    );
  });

  // req: ENF-I18N-003
  it('puts a narrow no-break space before ; : ! ? in every message, whatever the file holds', () => {
    const offenders = Object.keys(fr).filter((key) => BEFORE_PUNCTUATION.test(render(key as keyof typeof fr)));
    expect(offenders).toEqual([]);
    expect(t('footer.source_unknown')).toBe(`Code source${NNBSP}: adresse indisponible`);
  });

  // req: ENF-I18N-003
  it('never writes a plural with a parenthesis: fichier(s) is an ICU plural', () => {
    expect(Object.entries(fr).filter(([, text]) => /\w\((?:s|e|es)\)/.test(text))).toEqual([]);
  });

  // req: ENF-I18N-003
  it('does not retypeset what the user or a source gave as a value', () => {
    expect(t('toRate.rated', { title: 'Quoi : ça ?', age: '12+' })).toContain('« Quoi : ça ? »');
  });

  // req: ENF-I18N-001
  it('speaks English once the language is set, with English plurals and formats, then French again', () => {
    try {
      setUiLanguage('en');
      expect(t('library.total', { count: 1 })).toBe('1 Work');
      expect(t('library.total', { count: 2 })).toBe('2 Works');
      expect(t('library.total', { count: 0 })).toBe('No Works');
      expect(t('sheet.bindings.rank', { rank: 1234 })).toBe('Rank 1,234');
    } finally {
      setUiLanguage('fr');
    }
    expect(t('library.total', { count: 2 })).toBe('2 Œuvres');
  });

  // req: ENF-I18N-001
  it('carries the same keys in both catalogs, none empty', () => {
    expect(Object.keys(en).sort()).toEqual(Object.keys(fr).sort());
    expect(Object.entries(en).filter(([key, value]) => !value.trim() && fr[key as keyof typeof fr].trim())).toEqual([]);
  });

  // req: ENF-I18N-001
  it('picks the first browser language the catalogs speak, French when none', () => {
    expect(detectLanguage(['en-GB', 'fr'])).toBe('en');
    expect(detectLanguage(['de', 'fr-CA', 'en'])).toBe('fr');
    expect(detectLanguage(['de', 'es'])).toBe('fr');
    expect(detectLanguage(undefined)).toBe('fr');
  });
});
