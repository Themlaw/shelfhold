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

## Variantes de la direction

Les trois autres thèmes, `console`, `galerie` et `calme`, sont des **variantes de Papier et encre**,
pas des directions à part : mêmes polices (Alegreya, Alegreya Sans), mêmes illustrations et mêmes
couvertures, même rayon (1 px). Seuls changent la teinte du papier, l'encre et l'accent, et la
densité (la disposition des couvertures). Les identifiants stockés restent ceux des réglages
existants ; seuls les libellés affichés changent.

| Identifiant | Libellé (FR) | Papier | Accent (encre d'imprimerie) | Densité |
| --- | --- | --- | --- | --- |
| `bibliotheque` | Papier et encre | papier journal chaud | bleu des cahiers `#1F4A80` | couvertures en grille |
| `console` | Papier et encre — dense | gris froid | bleu nuit `#12305F` | couvertures en liste |
| `galerie` | Papier et encre — contraste | papier clair, encre presque noire | vert bouteille `#1E4D36` | couvertures en grille |
| `calme` | Papier et encre — sépia, sans animation | papier sépia | sépia `#7A3E12` | couvertures en liste ; aucun mouvement ni ombre |

Chacune a sa palette claire et sa palette sombre, passées au contrôle de contraste (CI,
`contrast.test.ts`) : texte 4,5:1, filets forts, focus et tablette 3:1. Pas de violet ni d'orange
vif : le rouge reste réservé à l'erreur.

## Composants

Dans `frontend/src/lib/ui/`, par niveau ; chacun a ses tests de comportement et d'accessibilité.

- **Atomes** (`atoms/`) : `Button` (primary, secondary, ghost, danger, danger-ghost ; sm, md,
  lg ; `loading`, `disabled` par `aria-disabled`, `href` pour un lien), `IconButton` (nom
  obligatoire), `Input`, `Textarea`, `Select` (natif), `Checkbox`, `Radio`, `Switch`, `Badge`,
  `Avatar` (ex-libris), `Segmented` (un choix parmi trois ou quatre, tous visibles : vrais boutons radio dessinés en segments, encre pleine pour le choix ; la politique d'une source, « Ajouter directement / Demander / Interdit »), `Link`, `Icon` (jeu maison `icons.ts`), `Spinner`, `Skeleton`, `Cover`,
  `Progress`, `ProgressRibbon` (le signet), `Illustration`.
- **Molécules** (`molecules/`) : `Field`, `SettingRow`, `SeriesCard`, `ChapterRow`, `TableRow`,
  `Alert`, `ProblemAlert`, `Toast`, `Dialog`, `ConfirmDialog`, `Menu`, `Tabs`, `SearchBar`, `Pagination`,
  `EmptyState`, `ErrorState`, `AgeScale` (l'échelle d'âge à crans d'un enfant : une règle de marches en boutons radio, la part sous le plafond encrée, celle au-dessus en pointillé, le signet bleu sur la marche choisie ; un plafond hors des marches reçoit sa propre marche ; `counts` imprime le nombre d'œuvres visibles par marche quand l'API le donne, ce qu'elle ne fait pas encore), `SecretOnce` (un secret montré une seule fois : cadre à filet d'alerte, valeurs en monospace, bouton « Copier », un seul geste pour le refermer ; la page garde la valeur en mémoire seulement).
- **Organismes** (`organisms/`) : `AppHeader`, `TableOfContents`, `MobileTabBar`, `Colophon`,
  `Shelf`, `DataTable`, `SettingsForm`, `ErrorScreen`, `PageHead` (le fil d'Ariane, le `h1`, le chapeau et les actions d'un écran d'exploitation) et `StatBar` (la bande de chiffres clés en tête d'un registre : étiquette, grand chiffre tabulaire, ligne de contexte ; un ton se dit par une icône et des mots).

`bits-ui` fournit le focus, le clavier et l'ARIA de `Dialog`, `Menu` et `Tabs`, et n'est importé
que par eux (ESLint). Ni thème de bibliothèque, ni Tailwind.

### Notes d'usage

- **`ProblemAlert`** dit une erreur du serveur avec les mots de l'interface : le problème écrit
  par le serveur (titre, cause, action, identifiant d'incident à communiquer), jamais son corps
  brut, avec un bouton « Réessayer » si `onretry` est donné. Un refus de session (401) ou de rôle
  (403), repéré par `blockKind(error)`, devient un `ErrorState` avec son dessin, pour ne pas se
  lire comme une panne de l'écran. Il remplace `ProblemCard` dans les écrans refaits.
- **Lecteur** : page à part (`/lire`, politique CSP propre) ; il charge la fondation (jetons et
  polices servis par l'instance). La planche d'une page d'images et la colonne webtoon posent
  sur `--cv-paper`, qui ne s'inverse pas en sombre ; le reste de la scène suit le thème. Les
  réglages sont une feuille du bas sous 48 rem et un panneau latéral au-delà ; ni l'un ni l'autre
  ne piège le focus.
- **`OfflineKeep`** a une variante `compact` (icônes seules, noms accessibles conservés) pour les
  barres du lecteur.

## Pages écrites par le serveur

La connexion (`/login`), la confirmation de liaison OIDC, le kit de secours et les pages `/lite`
sont du HTML écrit par le backend (`backend/src/shelfhold/interfaces/http_api/`, gabarits dans
`pages/` et `lite/templates/`). Leur politique de sécurité interdit tout code en ligne (ni
`<style>`, ni `style=""`, ni `<script>` en ligne) : elles lient des fichiers de leur propre origine.

- **Jetons et polices, un seul endroit.** `frontend/scripts/build-server-pages.ts` (`npm run
  pages:css`) compile le thème « Bibliothèque » (`tokens.json`, clair et sombre) et la fondation
  (`@font-face`, échelle, espacements, rayons, filets) dans
  `pages/paper-ink.tokens.css`, copie générée et commitée ; `scripts/server-pages.test.ts` échoue si
  elle diverge. Les fichiers de police sont ceux de `frontend/static/fonts/`, servis par le front
  sous `/fonts/` : aucune seconde copie, aucun CDN. À la modification d'un jeton ou d'un
  `@font-face`, relancer `npm run pages:css`.
- **Feuilles servies par le backend** (`page_assets.py`, publiques, `no-cache` avec `ETag`) :
  `/pages/paper-ink.css` (jetons, puis `pages/paper-ink.css` : base, champs, boutons, alerte,
  registre des faits, kit imprimable), `/lite/style.css` (jetons pour les polices seulement, puis
  `pages/lite.css`). Scripts : `/pages/appearance.js` (lit `shelfhold.appearance` : le choix clair ou
  sombre de l'appareil), `/pages/login.js`, `/pages/confirm.js`, `/login/passkey.js`. La politique est
  construite par `page_csp()` : `default-src 'none'`, `style-src 'self'`, `font-src 'self'`,
  `script-src 'self'` seulement si la page a des scripts, `form-action 'self'`, `base-uri 'none'`.
- **Connexion neutre** : ni nom d'outil, ni image, ni balise Open Graph, un lien vers le code source.
  Titre, une phrase, l'alerte (titre du problème et sa piste `action`), deux champs, la case « se
  souvenir de moi » avec ce qu'elle fait écrit dessous, un bouton principal, puis « ou » et les
  autres voies (clé d'accès, fournisseur d'identité) au même gabarit de bouton. La case cochée est la
  demande explicite que le profil durci attend : la page envoie `confirm_remember` avec `remember`.
- **Kit de secours** : une feuille à imprimer (`@media print` : encre sur blanc, couleurs du
  système), dans la langue de la personne qui l'ouvre.
- **`/lite`** : noir et blanc stricts (`#000` sur `#fff`, 21:1) quel que soit le thème, texte de
  20 px, cibles de 56 px (`3.5rem`), filets de 2 à 3 px, aucune ombre ni animation, zéro JavaScript.
  Le menu garde nom, profil, quatre portes (la courante pleine) et recherche ; le pied porte la
  déconnexion et le lien vers la version complète. Une erreur se distingue d'une note par un double
  filet, pas par une couleur. La pagination est une table d'une ligne (« Précédente · Page n ·
  Suivante », le côté absent en pointillés). Pas de variables CSS ni de grille : WebKit 538 et les
  Kindle anciens ne les connaissent pas ; seules les polices viennent du front, et elles se replient
  sur le serif de l'appareil (ils ne lisent pas le woff2).

## Règles

- **Notices** (`notify()` de `ui/toast.svelte.ts`) : une seule à la fois. Sur grand écran dans
  l'en-tête, sous 60 rem posée sur la barre d'onglets ; la mise en page garde sa hauteur libre
  sous le colophon : une notice ne masque jamais le contenu. Succès et info partent seules
  (6 s) ; un danger reste et s'annonce tout de suite (`role="alert"`).
- **Sombre** : conçu, pas inversé (« lampe de chevet ») ; choix clair, sombre ou système dans
  l'en-tête, gardé sur l'appareil et sur le profil.
- **Couvertures** : même ratio 2:3 partout. L'image réelle (vignette du serveur) remplace
  toujours la couverture composée. Sans image, la famille suit le type de l'œuvre donné par l'API
  (`kind`) : roman relié (toile, trois compositions et un motif gravé), collection blanche (double
  filet, titre en italique de couleur), manga (encre noire, bande de titre verticale, grand
  numéro), webtoon (couleurs pleines, titre dans une bande du bas), album BD (bandeau de titre,
  vignette « ligne claire » cernée). Les manga, webtoons et albums reçoivent une scène dessinée
  (`cover-art.ts`, tracée par `CoverArt.svelte`, jamais du HTML en chaîne) avec sa palette
  `--cv-*`. Composition, motif et scène sont tirés de l'identifiant de la série (`seed`), à
  défaut du titre : la même œuvre a toujours la même couverture. Sous 6,5 rem de large, la
  couverture n'est plus qu'une image et le titre est écrit dessous ou à côté ; sous 9 rem,
  l'auteur la quitte. Une œuvre réservée n'affiche jamais son titre.
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

## Écrans d'administration

Santé, sources, activité, réglages et modules sont des écrans *opérer* : `PageHead`, puis les sections
(`.section`, `.section-head`), des registres `DataTable` (chiffres tabulaires, fiches sous 600 px)
ou des listes `.entries`, et `Alert` pour tout message de la page. `ProblemCard`, `HealthEntry`,
`ReauthPrompt` et `TypedConfirm` ne sont plus que des habillages de ces composants. Ce qui ne se défait
pas (retrait d'un index ou d'un webhook, retour arrière d'un module, fusion d'œuvres ou d'éditions)
passe toujours par `ConfirmDialog` ; la confirmation par phrase recopiée d'une destination reste en
ligne (`TypedConfirm`), parce que le serveur donne la phrase après un aperçu. Un bouton qui n'est pas
encore permis est `aria-disabled` (jamais `disabled`) : il garde le focus et dit pourquoi.
