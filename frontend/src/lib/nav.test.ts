import { describe, expect, it } from 'vitest';
import { navItems } from './nav';

const FAMILY = ['home', 'search', 'requests'];
const ids = (list: { id: string }[]) => list.map((i) => i.id);

describe('navigation by role (EF-UX-005)', () => {
  // req: EF-UX-005
  it('test_nav_by_role: the tabs are the ids the server answered, in its order', () => {
    expect(ids(navItems(FAMILY))).toEqual(FAMILY);
    expect(ids(navItems([...FAMILY, 'approve', 'activity', 'settings']))).toEqual([
      ...FAMILY,
      'approve',
      'activity',
      'settings'
    ]);
  });

  // req: EF-UX-005
  it('offers nothing of its own: no ids from the server, no tabs', () => {
    expect(navItems([])).toEqual([]);
  });

  // req: EF-UX-005
  it('drops an unknown id, a duplicate and anything that is not a destination of this client', () => {
    expect(ids(navItems(['home', 'home', 'admin', '__proto__', 'constructor', 'toString']))).toEqual([
      'home'
    ]);
  });

  // req: EF-UX-005
  it('M3-lite: one setting shrinks the family navigation to Accueil and Chercher', () => {
    expect(ids(navItems(FAMILY, true))).toEqual(['home', 'search']);
    expect(ids(navItems([...FAMILY, 'activity', 'settings'], true))).toEqual([
      'home',
      'search',
      'activity',
      'settings'
    ]);
  });
});
