import { describe, expect, it } from 'vitest';
import { navByRole } from './nav';

function labels(items: { label: string }[]): string[] {
  return items.map((i) => i.label);
}

describe('navByRole (EF-UX-005)', () => {
  it('test_nav_by_role: family sees Accueil, Chercher, Mes demandes', () => {
    const family = navByRole('member');
    expect(labels(family)).toEqual(['Accueil', 'Chercher', 'Mes demandes']);
  });

  it('admin sees the family tabs plus Activité and Réglages', () => {
    const admin = navByRole('admin');
    expect(labels(admin)).toEqual(['Accueil', 'Chercher', 'Mes demandes', 'Activité', 'Réglages']);
  });

  it('in M3-lite the family navigation shrinks to two tabs by a single setting', () => {
    const lite = navByRole('member', { reduced: true });
    expect(labels(lite)).toEqual(['Accueil', 'Mes demandes']);
  });
});
