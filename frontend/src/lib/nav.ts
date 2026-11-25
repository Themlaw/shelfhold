// Navigation by role (EF-UX-005). The server decides what a role is offered: `GET /api/v1/me`
// answers a list of destination ids (`nav`), and the screen only maps them to a label and a link.
// An id this client does not know is dropped; the screen never adds a destination of its own, so
// the admin tabs cannot appear for someone the server did not give them to (the routes behind them
// are refused to other roles by the server in any case).
import type { MessageKey } from '#lib/i18n/index.ts';
import type { IconName } from '#lib/ui/atoms/icons.ts';

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

// ---------------------------------------------------------------------------------------------
// The table of contents (docs/design/papier-et-encre.md, « Navigation »). The tabs above stay
// exactly the server's (EF-UX-005: a member gets three). Around them, the table of contents
// shelves every screen that has no other way in (the library, the saved selections, the
// collections, the offline copies, the modules, the passkeys, the trackers), each tied to
// something the server decided, never to a guess of the screen:
// - the places of one's own reading and account go to anyone signed in (the server gave tabs);
//   a child gets the reading ones only, the account ones being set up by a parent;
// - the places of the administration hang under the `settings` tab, which the server gives to
//   the admin role alone (the routes behind them refuse every other role in any case).
export type Role = 'admin' | 'member' | 'child' | null;

export type PlaceId =
  | NavId
  | 'library'
  | 'selections'
  | 'collections'
  | 'offline'
  | 'profiles'
  | 'health'
  | 'modules'
  | 'devices'
  | 'passkeys'
  | 'trackers';

export interface Place {
  id: PlaceId;
  href: string;
  labelKey: MessageKey;
  icon: IconName;
}

export interface PlaceGroup {
  id: 'read' | 'family' | 'instance' | 'account';
  labelKey: MessageKey;
  items: Place[];
}

const PLACES: Record<PlaceId, Place> = {
  home: { id: 'home', href: '/', labelKey: 'nav.home', icon: 'home' },
  library: { id: 'library', href: '/bibliotheque', labelKey: 'nav.library', icon: 'books' },
  selections: { id: 'selections', href: '/bibliotheque/selections', labelKey: 'nav.selections', icon: 'filter' },
  collections: { id: 'collections', href: '/collections', labelKey: 'nav.collections', icon: 'list' },
  search: { id: 'search', href: '/chercher', labelKey: 'nav.search', icon: 'search' },
  requests: { id: 'requests', href: '/mes-demandes', labelKey: 'nav.requests', icon: 'inbox' },
  offline: { id: 'offline', href: '/hors-ligne', labelKey: 'nav.offline', icon: 'offline' },
  approve: { id: 'approve', href: '/a-approuver', labelKey: 'nav.approve', icon: 'stamp' },
  profiles: { id: 'profiles', href: '/reglages/profils', labelKey: 'nav.profiles', icon: 'users' },
  activity: { id: 'activity', href: '/activite', labelKey: 'nav.activity', icon: 'tasks' },
  health: { id: 'health', href: '/sante', labelKey: 'nav.health', icon: 'pulse' },
  settings: { id: 'settings', href: '/reglages', labelKey: 'nav.settings', icon: 'gear' },
  modules: { id: 'modules', href: '/reglages/modules', labelKey: 'nav.modules', icon: 'plug' },
  devices: { id: 'devices', href: '/appareils', labelKey: 'nav.devices', icon: 'device' },
  passkeys: { id: 'passkeys', href: '/compte/passkeys', labelKey: 'nav.passkeys', icon: 'key' },
  trackers: { id: 'trackers', href: '/compte/trackers', labelKey: 'nav.trackers', icon: 'bookmark' }
};

/** The role of `/api/v1/me`, as this client knows it; anything else is nobody. */
export function roleOf(value: unknown): Role {
  return value === 'admin' || value === 'member' || value === 'child' ? value : null;
}

/** The groups of the table of contents for the tabs `ids` the server answered and the role. */
export function tableOfContents(ids: readonly string[], role: Role, lite: boolean = FAMILY_NAV_LITE): PlaceGroup[] {
  const tabs = new Set(navItems(ids, lite).map((item) => item.id));
  if (tabs.size === 0) return []; // nobody signed in: no place at all
  const admin = tabs.has('settings');
  const pick = (list: (PlaceId | false)[]): Place[] =>
    list.filter((id): id is PlaceId => id !== false).map((id) => PLACES[id]);
  const child = role === 'child';
  const groups: PlaceGroup[] = [
    {
      id: 'read',
      labelKey: 'nav.group.read',
      items: pick([
        tabs.has('home') && 'home',
        'library',
        !child && 'selections',
        !child && 'collections',
        tabs.has('search') && 'search',
        tabs.has('requests') && 'requests',
        'offline'
      ])
    },
    { id: 'family', labelKey: 'nav.group.family', items: pick([tabs.has('approve') && 'approve', admin && 'profiles']) },
    {
      id: 'instance',
      labelKey: 'nav.group.instance',
      items: pick([tabs.has('activity') && 'activity', admin && 'health', admin && 'settings', admin && 'modules'])
    },
    {
      id: 'account',
      labelKey: 'nav.group.account',
      items: pick([!child && 'devices', !child && 'passkeys', !child && 'trackers'])
    }
  ];
  return groups.filter((group) => group.items.length > 0);
}

/** The tabs of the bar at the bottom of a phone: four places, the fifth button opens the table. */
export function tabBarPlaces(ids: readonly string[], role: Role, lite: boolean = FAMILY_NAV_LITE): Place[] {
  const all = tableOfContents(ids, role, lite).flatMap((group) => group.items);
  const has = (id: PlaceId) => all.some((place) => place.id === id);
  const order: PlaceId[] = ['home', 'library', 'search', has('approve') ? 'approve' : 'requests'];
  return order.filter(has).map((id) => PLACES[id]);
}

/** The place `pathname` belongs to: the longest href that is a prefix of it (`/` only itself). */
export function currentPlace(pathname: string, places: readonly Place[]): PlaceId | null {
  let best: Place | null = null;
  for (const place of places) {
    const inside = place.href === '/' ? pathname === '/' : pathname === place.href || pathname.startsWith(`${place.href}/`);
    if (inside && (!best || place.href.length > best.href.length)) best = place;
  }
  return best?.id ?? null;
}
