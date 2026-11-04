import { describe, expect, it } from 'vitest';
import { t } from './index';

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
});
