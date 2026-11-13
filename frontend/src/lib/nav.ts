// Navigation by role (EF-UX-005). The server decides what a role is offered: `GET /api/v1/me`
// answers a list of destination ids (`nav`), and the screen only maps them to a label and a link.
// An id this client does not know is dropped; the screen never adds a destination of its own, so
// the admin tabs cannot appear for someone the server did not give them to (the routes behind them
// are refused to other roles by the server in any case).
import type { MessageKey } from '#lib/i18n/index.ts';

export type NavId = 'home' | 'search' | 'requests' | 'approve' | 'activity' | 'settings';

export interface NavItem {
  id: NavId;
  href: string;
  labelKey: MessageKey;
}

const DESTINATIONS: Record<NavId, NavItem> = {
  home: { id: 'home', href: '/', labelKey: 'nav.home' },
  search: { id: 'search', href: '/chercher', labelKey: 'nav.search' },
  requests: { id: 'requests', href: '/mes-demandes', labelKey: 'nav.requests' },
  approve: { id: 'approve', href: '/a-approuver', labelKey: 'nav.approve' },
  activity: { id: 'activity', href: '/activite', labelKey: 'nav.activity' },
  settings: { id: 'settings', href: '/reglages', labelKey: 'nav.settings' }
};

/** M3-lite (plan, D1): the one setting that shrinks the family navigation to two tabs, Accueil and
 * Chercher. The admin tabs are not affected. */
export const FAMILY_NAV_LITE = false;
const LITE_HIDDEN: readonly NavId[] = ['requests'];

function isNavId(id: string): id is NavId {
  return Object.hasOwn(DESTINATIONS, id);
}

/** The tabs of `ids` (as answered by the server), in the server's order, without duplicates. */
export function navItems(ids: readonly string[], lite: boolean = FAMILY_NAV_LITE): NavItem[] {
  const seen = new Set<NavId>();
  const items: NavItem[] = [];
  for (const id of ids) {
    if (!isNavId(id) || seen.has(id) || (lite && LITE_HIDDEN.includes(id))) continue;
    seen.add(id);
    items.push(DESTINATIONS[id]);
  }
  return items;
}
