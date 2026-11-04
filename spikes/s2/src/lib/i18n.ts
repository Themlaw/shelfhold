import { IntlMessageFormat } from 'intl-messageformat';
const fr: Record<string, string> = {
  title: 'Shelfhold, spike S2',
  health: 'État du serveur : {state}',
  count: '{n, plural, =0 {Aucune œuvre} one {# œuvre} other {# œuvres}}',
  nav_home: 'Accueil', nav_lib: 'Bibliothèque', nav_settings: 'Réglages',
  theme: 'Thème', save: 'Enregistrer'
};
export function t(key: string, values?: Record<string, string | number>): string {
  return new IntlMessageFormat(fr[key], 'fr').format(values) as string;
}
