# Direction visuelle « Papier et encre »

L'interface de Shelfhold est composée comme un livre de bibliothèque : papier chaud, encre presque
noire, une seule encre bleue pour l'action, des filets plutôt que des ombres. Deux signatures, et
pas d'autre audace : **les couvertures générées** (jamais de carré gris) et **la table des
matières** (la navigation, avec ses points de conduite et ses folios). Le reste est discipliné.

Trois modes, choisis par écran : *lire* (fiche, liseuse : le texte d'abord), *opérer* (réglages,
registres : contrôles standard, densité), *persuader* (rien pour l'instant).

## Jetons

| Fichier | Contenu |
| --- | --- |
| `frontend/src/lib/themes/<thème>/tokens.json` | Couleurs par rôle, en clair et en sombre (compilées en `--color-*` dans `themes.css`), polices, rayon des couvertures. « Papier et encre » est le thème par défaut `bibliotheque`. |
| `frontend/src/lib/themes/foundation.css` | Tout ce qui n'est pas une couleur, commun aux thèmes : `@font-face`, échelle typographique `--step-*`, espacements `--space-1` à `--space-9` (base 4), rayons, filets `--rule-*`, ombres `--shadow-*` (teintées d'encre ; en sombre, la profondeur vient de la luminance), mouvement `--dur-*` / `--ease-*` (à 0 sous `prefers-reduced-motion`), couches `--z-*`, mise en page, papiers des couvertures `--cover-*` / `--cv-*`. |
| `frontend/src/routes/global.css` | La base (corps, titres, liens, focus) et les utilitaires de page : `.page`, `.page--form`, `.page--read`, `.running-head`, `.page-head`, `.page-title`, `.lede`, `.section`, `.section-head`, `.prose`, `.stack`, `.btn-row`, `.cols`, `.panel`, `.entries`, `.meta-list`, `.visually-hidden`, `.hint`, `.num`, `.folio`. |

Rôles de couleur : `bg`, `surface`, `surface-2`, `surface-chrome` (en-tête), `surface-float`
(menus, dialogues), `ink`, `muted`, `subtle`, `rule`, `rule-strong`, `accent`, `on-accent`,
`accent-strong`, `accent-text`, `accent-soft`, `ok`, `warn`, `err` (et leurs `*-soft`), `on-err`,
`focus`, `shelf`, `overlay`. Un composant ne lit que des variables : aucun hexadécimal, aucune
longueur magique.

Polices : Alegreya (texte, titres, folios) et Alegreya Sans (interface), servies par l'instance
(`frontend/static/fonts/`, licence OFL dans `OFL.txt`), jamais par un CDN. Le code et les
identifiants sont en monospace système.

Contraste mesuré par `node scripts/contrast.ts` (CI, `contrast.test.ts`) : texte 4,5:1 sur tous
les fonds de page, 3:1 pour les filets forts, le focus et la tablette. Thème par défaut, clair :
encre 14,6:1, gris 7,9:1, gris discret 6,0:1, bleu 7,6:1 ; sombre : 14,3:1, 9,2:1, 6,4:1, 10,1:1.

## Composants

Dans `frontend/src/lib/ui/`, par niveau ; chacun a ses tests de comportement et d'accessibilité.

- **Atomes** (`atoms/`) : `Button` (primary, secondary, ghost, danger, danger-ghost ; sm, md,
  lg ; `loading`, `disabled` par `aria-disabled`, `href` pour un lien), `IconButton` (nom
  obligatoire), `Input`, `Textarea`, `Select` (natif), `Checkbox`, `Radio`, `Switch`, `Badge`,
  `Avatar` (ex-libris), `Link`, `Icon` (jeu maison `icons.ts`), `Spinner`, `Skeleton`, `Cover`,
  `Progress`, `ProgressRibbon` (le signet), `Illustration`.
- **Molécules** (`molecules/`) : `Field`, `SettingRow`, `SeriesCard`, `ChapterRow`, `TableRow`,
  `Alert`, `Toast`, `Dialog`, `ConfirmDialog`, `Menu`, `Tabs`, `SearchBar`, `Pagination`,
  `EmptyState`, `ErrorState`.
- **Organismes** (`organisms/`) : `AppHeader`, `TableOfContents`, `MobileTabBar`, `Colophon`,
  `Shelf`, `DataTable`, `SettingsForm`, `ErrorScreen`.

`bits-ui` fournit le focus, le clavier et l'ARIA de `Dialog`, `Menu` et `Tabs`, et n'est importé
que par eux (ESLint). Ni thème de bibliothèque, ni Tailwind.

## Règles

- **Notices** (`notify()` de `ui/toast.svelte.ts`) : une seule à la fois. Sur grand écran dans
  l'en-tête, sous 60 rem posée sur la barre d'onglets ; la mise en page garde sa hauteur libre
  sous le colophon : une notice ne masque jamais le contenu. Succès et info partent seules
  (6 s) ; un danger reste et s'annonce tout de suite (`role="alert"`).
- **Sombre** : conçu, pas inversé (« lampe de chevet ») ; choix clair, sombre ou système dans
  l'en-tête, gardé sur l'appareil et sur le profil.
- **Couvertures** : même ratio 2:3 partout. L'image réelle (vignette du serveur) quand elle
  existe ; sinon une couverture dessinée à partir du titre (famille selon le type : roman,
  manga, webtoon, BD), stable d'une visite à l'autre. Une œuvre réservée n'affiche jamais son
  titre.
- **CSP** : pas d'attribut `style=""` dans le balisage ; une valeur calculée passe par une
  directive `style:` de Svelte (appliquée par le DOM, autorisée par `style-src 'self'`).
- **Accessibilité** : cibles de 44 px, focus visible partout, un seul `h1` par écran, le statut
  dit par une icône et un mot (jamais la couleur seule), tableaux en fiches sous 600 px.
- **Pages d'erreur** : dans la coquille (en-tête, table des matières, colophon), avec un titre
  du domaine (« Ce volume n'est pas sur l'étagère »), une cause probable et une sortie ; jamais le
  message brut du serveur.

## Grille de relecture d'un écran

1. Les états sont tous là : nominal, vide, filtre sans résultat, chargement (squelette fidèle),
   erreur partielle, 401, 403, 404, 500, hors ligne, liste longue, texte long, mobile, sombre,
   clavier.
2. Un seul `h1`, une seule action principale, le libellé du bouton répète le titre.
3. Rien en dur : libellés dans les catalogues FR et EN, valeurs dans les jetons.
4. Contraste AA mesuré, focus visible, cibles de 44 px, pas de défilement horizontal à 360 px.
5. Captures regardées à 1440 et 390 px, en clair et en sombre.
