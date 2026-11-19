---
status: acceptée (décision déléguée au mainteneur, plan M5 D-P1/D-P2)
date: 2026-11-17
decision-makers: mainteneur
---
# ADR-0055 — Lecteur intégré de M5 : rôle, chemin du contenu, port `ReaderEngine`

## Contexte et problème
L'ADR-0012 décide d'un lecteur intégré secondaire livré en M5, mais laisse ouverts trois points que M5 ne peut pas coder sans réponse :
- **son rôle exact** (06 Q22, ouverte « début M5 ») : aperçu, concurrent de Kavita, ou lecteur du seul contenu non exporté ;
- **l'isolation du contenu** : 01 (EF-LEC-005, ENF-SEC-004) accepte « une origine **ou** un chemin dédié », ce qui demande un DNS, un certificat et un proxy de plus si l'on choisit l'origine ;
- **la forme du port `ReaderEngine`** et les moteurs qu'il enregistre, alors que 03 range `test_pdfjs_is_eval_supported_false` en M5 pour un format (PDF) que 01 (EF-LEC-004) date « Plus tard ».

S'y ajoutent des contradictions relevées à l'extraction des livrables : 01 (EF-LEC-009) et 03 demandent pour kosync un `409` sur hash ambigu et un repli par `metadata`, alors que l'ADR-0053, acceptée et déjà livrée en M4 (L47, L50), impose `200` à tout `PUT` et « ne devine jamais » ; 02 donne à `x-auth-key` le md5 du mot de passe alors que l'ADR-0053 le fixe au md5 du secret du jeton d'appareil ; 02 annonce « OIDC en V2 » alors que l'ADR-0018 le place en M5 ; 01 date ENF-A11Y-002 de M3 seule et EF-EXP-058 de M1a seule alors que 03 leur donne un volet M5.

## Facteurs de décision
- Kavita reste le lecteur principal de la famille (01 §1, point 4 ; ADR-0020) : Shelfhold ne devient pas un second Kavita.
- La définition de fini de M5 : « lire un extrait dans Shelfhold sans exporter ».
- Contenu non fiable (ADR-0047, ENF-SEC-004) : aucun script de livre ne s'exécute, tout octet sort par `content_response`, le lecteur lit le **dérivé neutralisé**.
- Contrôle parental (ADR-0027, ENF-SEC-010) : chaque route de contenu est revérifiée contre la visibilité du profil à chaque service.
- KOReader arrête sa file d'envoi au premier statut autre que 200 (ADR-0053, amendement du 2026-11-16) : un relais qui répond autre chose casse la synchronisation des liseuses.
- Peu d'infrastructure à la charge du porteur : pas d'origine, de DNS ni de certificat supplémentaires par défaut.

## Décisions du porteur (D-P1, D-P2) et décisions déléguées (D6 à D9)

### D-P1 — Rôle du lecteur (06 Q22, tranchée)
**Option 3.** Le lecteur est **complet pour le contenu non exporté** (copie conservée, ADR-0028) : lecture, réglages, position enregistrée sur l'ancre `content` (ADR-0045, EF-LEC-014). Pour **tout autre contenu**, il propose « **Lire un extrait** ». **Kavita reste le lecteur principal.**

Un contenu **exporté vers une destination que le profil peut lire** (par exemple Kavita avec un compte lié) se lit là-bas ; Shelfhold n'ouvre pas un second lieu de lecture complet pour lui. Pour un profil sans accès à la destination (pas de compte Kavita, export vers Komga, CWA, un arbre ou une liaison `pull` seulement), le lecteur complet reste disponible sur la copie conservée. Une Œuvre lue en copie conservée puis exportée **garde sa position sur l'ancre `content`** : l'invariant I30 interdit de la recoller sur l'`export_item`, et « Continuer la lecture » (L14) l'affiche comme telle.

Écartées : l'aperçu seul (peu utile hors atelier) et le lecteur complet de tout le catalogue (concurrence frontale avec Kavita, deux lieux de lecture, conflits de progression).

#### « Lire un extrait » : borné côté serveur, n'écrit rien
- **Borne** : le **premier chapitre** (première entrée de la table des matières qui n'est ni couverture ni page de garde), ou à défaut les **N premières pages** (réglable, **N = 10** par défaut ; pour un EPUB sans chapitre exploitable, l'équivalent de N pages de 1 800 caractères). Dans tous les cas, jamais plus de **20 %** de la publication (valeur par défaut, réglable).
- **Extrait EPUB = dérivé d'extrait coupé côté serveur** (N3). `/content` filtre par entrée : une liste d'entrées ne peut pas borner un premier chapitre qui pèse 60 % du livre, ni un EPUB à un seul fichier. Le serveur produit donc un **dérivé d'extrait** : spine tronqué à la borne, dernier document coupé proprement par `safe_parse_xml`, ressources réduites à celles qu'il référence ; il est mis en cache par (support, version du neutraliseur, borne). Si la coupe est **impossible dans la borne**, la réponse est un **refus typé « extrait indisponible »** (jamais le fichier entier). Pour les supports d'images, la borne s'applique à la liste de pages. Le test `test_excerpt_publication_is_bounded` (L06) reçoit les cas « EPUB à un seul fichier » et « premier chapitre au-delà de 20 % ».
- **Le ticket porte la portée** (`full` ou `excerpt`) **et la liste fermée des entrées autorisées**. `/content` refuse toute page ou ressource hors de cette liste (`404`, sans oracle d'existence), ressources liées comprises : une image, une police ou une feuille de style n'est servie que si un document autorisé la référence. Un client qui forge `/content/<ticket>/…/ch42.xhtml` ou une page hors borne n'obtient rien.
- **Aucune écriture** : ni `reading_position`, ni historique, ni `reading_conflict`, ni état « en cours », ni renvoi vers Kavita ou kosync. `PUT /api/reader/positions` refuse une publication ou un ticket de portée `excerpt`.
- **Liste fermée** : calculée **côté serveur, à l'émission**, depuis le dérivé, par `safe_parse_xml` (`@import` en chaîne, `srcset` et `xlink:href` compris) ; elle vit côté serveur, le ticket étant un identifiant opaque (pas dans le chemin) ; l'OPF et le document de navigation en font partie. Une section hors borne répond `404` ; foliate affiche alors une erreur typée « fin de l'extrait », jamais une page blanche.
- Tests : `test_content_excerpt_ticket_refuses_out_of_bound_entry` (L05), `test_excerpt_publication_is_bounded` (L06), `test_excerpt_writes_no_position` et `test_excerpt_never_pushed_to_kavita` (L07).

### D-P2 — Chemin du contenu (EF-LEC-005, ENF-SEC-004)
Le contenu est servi sur **`/content/*`, même origine**, isolé par une **CSP `sandbox` sur chaque réponse**. Une **origine séparée** reste possible plus tard (option documentée ci-dessous), jamais exigée.

#### Ce que protège la CSP `sandbox` de `/content/*`, et ce qu'elle ne protège pas
Elle protège la **navigation directe** vers une URL de contenu (onglet, lien copié) : le document s'y ouvre dans une origine opaque, sans cookie ni script. Elle **ne protège pas le rendu dans le lecteur** : les octets sont lus par `fetch`, puis passent en `blob:`, et les en-têtes de la réponse sont perdus. Le rendu repose donc sur les mécanismes suivants.

#### Modèle de rendu (corrigé après relecture adverse)
- **Images (manga, webtoon, CBZ)** : aucune iframe. Les pages sont des `<img>` (ou `createImageBitmap`) alimentés par des `blob:` d'images matricielles.
- **EPUB (foliate-js)** : foliate-js pose lui-même `sandbox="allow-same-origin allow-scripts"` sur l'iframe et lit `iframe.contentDocument` pour charger, paginer et calculer les CFI. Son README rend `allow-scripts` obligatoire à cause du bogue WebKit 218086 (clic, toucher et clavier). **Le sous-module n'est pas patché** : l'iframe sandbox sans ces drapeaux, voulue à l'origine, est **abandonnée** pour l'EPUB. Les documents du livre sont des `blob:` créés par la SPA : ils sont **de l'origine de la SPA**. Les drapeaux `allow-top-navigation*` et `allow-popups` ne sont **jamais** ajoutés.
- **Invariant : aucun script d'un livre ne s'exécute, jamais**, quel que soit le niveau de confiance de l'EPUB (`untrusted` ou `publisher`). Un script qui s'exécuterait aurait les droits de la SPA : lecture du cookie CSRF, appels de toute l'API au nom du profil, ticket `full` qui contourne l'extrait, création d'une clé d'API. L'invariant tient sur **deux barrières indépendantes**, chacune suffisante :
  1. **la CSP héritée** par le document `blob:` (tableau ci-dessous) ;
  2. **le filtre de contenu client**, appliqué à tout EPUB avant la création du `blob:` (section « Filtre de contenu »), et, pour un EPUB `untrusted`, **le neutraliseur serveur** (ADR-0047), qui fait double emploi avec lui, volontairement.
- Test de l'invariant : `test_epub_script_never_runs` sur Chromium, Firefox et WebKit, avec en plus un cas « EPUB `publisher`, CSP du lecteur retirée » : le filtre seul doit empêcher toute exécution, ce qui prouve l'indépendance des barrières.
- **Défense en profondeur** : même si un script s'exécutait, les routes sensibles (création et rotation de clés d'API, contrôle parental, jetons d'appareil) sont des actions réservées qui exigent une **ré-authentification récente** (ADR-0049, ENF-SEC-010) et le code de l'entrée `/lire` ne les appelle pas (aucun accès depuis le lecteur). **Limite écrite** : la fenêtre de ré-authentification (`fresh_auth_s`, 300 s par défaut, ADR-0049) fait que, dans les cinq minutes qui suivent une connexion, la session est fraîche et cette défense ne joue pas ; le contrôle parental n'est réservé que si un profil restreint existe, et la demande d'un ticket `full` n'est pas une action réservée. La défense en profondeur ne remplace donc pas les deux barrières. Test : `test_reserved_routes_require_fresh_auth_from_reader_session` (L05), sur une session dont `auth_age_s` **dépasse** `fresh_auth_s`.

#### Navigateurs pris en charge par le moteur EPUB
Le moteur EPUB exige l'héritage de la CSP par les `blob:` et `'strict-dynamic'` : **Safari et iOS 16.4 au moins** (WebKit n'applique l'héritage de la CSP du créateur que depuis octobre 2022, bogues 245682 et 224745 ; `'strict-dynamic'` depuis Safari 15.4 ; **16.4** parce que `vendor/zip.js` est le build `zip-core` de foliate-js, sans codec d'inflation JavaScript et sans worker : un EPUB compressé exige `DecompressionStream('deflate-raw')`, présent dans Safari à partir de 16.4 ; amendé le 2026-11-18, L11 #370), **Chromium 110 et Firefox 115 au moins** (valeurs proposées, à confirmer par le test de L12). **Détection** : le registre lit les Client Hints (`Sec-CH-UA`) quand ils existent, sinon la chaîne UA, et en cas de doute **refuse** le moteur EPUB. Au-dessous de la version minimale, le lecteur propose Kavita. Le moteur d'images (sans iframe) n'est pas concerné. Test : `test_registry_refuses_epub_engine_below_min_browser` (L12).

**Test d'héritage (L12, trois moteurs)** : `test_blob_document_inherits_reader_csp` charge un `blob:` de premier niveau, une iframe `blob:` imbriquée dans le livre (second niveau) et un `srcdoc`, et vérifie l'exécution de script **et** `img-src`. **Si le test échoue sur un moteur ciblé, L12 ne fusionne pas.** (L11 ne livre que le sous-module : il ne porte ni le lecteur ni sa CSP.)

#### CSP du lecteur, directive par directive
`/lire` est une **entrée HTML séparée**, **pas une route SvelteKit** : une route de la SPA porterait la `<meta http-equiv>` CSP de la SPA (`style-src 'self'`, que `spa_csp` impose au démarrage), les deux politiques s'appliqueraient ensemble et la CSP du tableau ne serait jamais la CSP effective. Règles :
- **entrée de build distincte**, hors du routeur SvelteKit (`frontend/` : entrée `reader.html` et son module), **sans** `<meta>` CSP (la CSP vient de l'en-tête, ou d'une `<meta>` strictement identique) ;
- **garde au démarrage symétrique de `spa_csp`** (`reader_csp`) : élargissements admis par rapport à la SPA, `style-src` avec `blob:` et `'unsafe-inline'` et `style-src-attr`, **rien d'autre** ; tout autre écart refuse de démarrer ;
- **on y entre par navigation complète**, jamais par le routeur client : tout lien vers `/lire` force un chargement de document (`data-sveltekit-reload`, ou `<a>` hors routeur) ;
- **le lecteur refuse de monter** si la CSP effective du document n'est pas celle du lecteur (lecture de `securitypolicyviolation` et sonde d'une ressource interdite au démarrage) ;
- **le service worker de L16a ne sert jamais la coquille de la SPA pour `/lire`** : `/lire` est exclu du repli de navigation, et mis en cache avec ses en-têtes, par la copie hors ligne de L16b seulement ;
- **la CSP de la SPA n'est pas élargie** ; la page `/lire` ne reçoit que sa propre CSP.

Tests : `test_reader_refuses_to_mount_under_spa_csp` (navigation client vers `/lire`), `test_reader_entry_has_no_spa_meta_csp`, `test_offline_reader_entry_keeps_reader_csp` (L12, L16a).

Un document `blob:` hérite de la CSP du document qui l'a créé :

| Directive | Valeur | Remarque |
|---|---|---|
| `default-src` | `'none'` | |
| `script-src` | `'sha256-<amorçage>' 'strict-dynamic'` | **sans** `'self'`, **sans** hôte, **jamais** `blob:`, `'unsafe-inline'` ni `'unsafe-eval'` : un script inséré par l'analyseur (tous ceux d'un livre) est refusé, les imports dynamiques du script d'amorçage restent admis ; vaut **sans condition** (indépendant de l'URL publique) ; **condition de validité** : voir « Condition de `'strict-dynamic'` » |
| `style-src`, `style-src-elem` | `'self' blob: 'unsafe-inline'` | règle pour les styles d'EPUB **neutralisés** (`<style>` en ligne et feuilles `blob:` du livre, après filtre : ni `@import`, ni `url()` distant) ; élargissement assumé, limité à l'entrée `/lire` et vérifié par `reader_csp` |
| `style-src-attr` | `'unsafe-inline'` | attributs `style=` omniprésents dans les EPUB |
| `img-src` | `'self' blob:` | jamais `data:`, jamais d'hôte distant |
| `font-src` | `'self' blob:` | jamais `data:`, jamais `*` |
| `media-src` | `'none'` | le lecteur ne joue ni audio ni vidéo (relecture adverse de #381, C3) ; jamais `*` |
| `frame-src` | `blob:` | |
| `worker-src` | `'none'` | `/lire` n'enregistre aucun service worker ni worker ; l'enregistrement de la PWA reste dans la SPA (L16a) |
| `manifest-src` | `'none'` | le manifeste de la PWA n'est pas lié depuis `/lire` |
| `connect-src` | `'self'` | pour `fetch` de `/content/*` |
| `object-src`, `base-uri`, `form-action` | `'none'` | |
| `frame-ancestors` | `'none'` | ajouté par l'intergiciel |
| `Referrer-Policy` | `no-referrer` | |

**Origines distantes interdites** : toutes. `@import` et `url()` distants dans une CSS ou un attribut `style`, `<link rel="dns-prefetch|preconnect|prefetch">` et toute ressource distante sont refusés. Les métadonnées de la publication (titre, auteur) affichées par `/lire` sont **toujours insérées comme texte**, jamais par `{@html}` (le `style-src 'unsafe-inline'` vaut pour la page elle-même) ; test `test_reader_metadata_rendered_as_text` (L12).

**Condition de `'strict-dynamic'`.** Sous `'strict-dynamic'`, seul un script **inséré par l'analyseur** est refusé. Un `<script>` qui échapperait au filtre puis serait cloné, importé ou rattaché par une API DOM depuis un script de confiance (`importNode`, `cloneNode` puis `append`, `range.extractContents` puis réinsertion) s'exécuterait : le document du livre est de même origine et l'iframe a `allow-scripts`. La barrière CSP n'est donc valable que si **aucun code de confiance** (foliate, adaptateur, relais clavier, L13) ne clone, n'importe ni ne réinsère un nœud du livre dans un document ; les notes de bas de page passent par un **nouveau document `blob:` filtré**.
- **Audit en L11** des chemins de foliate au commit épinglé qui déplacent des nœuds (`footnotes`, surlignage et `overlayer`, `search`), consigné dans `docs/dependencies.md` ; tout chemin trouvé est écarté ou passe par le filtre.
- `test_epub_script_never_runs` reçoit le cas « script échappé au filtre puis déplacé par une API DOM » : il doit **échouer sous la seule CSP** (pour documenter la limite) et **réussir avec le filtre**. Les deux barrières sont donc indépendantes pour tout vecteur sauf celui-ci, où le filtre est la barrière.
- Facultatif, Chromium seulement : `require-trusted-types-for 'script'` sur `/lire` ferme ce cas.
- **Amorçage (amendé le 2026-11-18, L12, mesuré sur Chromium)** : un `import()` lancé par le script en ligne haché est **refusé** sous `'strict-dynamic'` (la requête hérite de la métadonnée « inséré par l'analyseur »). Le script d'amorçage de `/lire` ajoute donc un `<script type="module">` ; ce module, et tout ce qu'il importe, est de confiance. Un nœud `<script>` déjà bloqué est marqué comme lancé : `importNode` et `cloneNode` ne l'exécutent pas, du balisage réécrit (`createContextualFragment`) si.
- Les `<link rel="modulepreload">` du HTML de `/lire` sont **filtrés du build** : sous `'strict-dynamic'`, `'self'` est ignoré et ils seraient refusés (rapports de violation bruyants pour les tests qui écoutent `securitypolicyviolation`).

**Invariant pour toute l'origine** : aucune route ne sert des octets fournis par un tiers avec un type JavaScript (`test_no_route_serves_untrusted_bytes_as_script`).

Tests : `test_reader_csp_font_media_frame_blob` (les directives du tableau, y compris `img-src blob:`, et l'absence de `'self'` et de `blob:` dans `script-src`) et `test_blob_document_inherits_reader_csp` (L11, L12).

#### Types servis par `/content`
`content_response` **ne sert jamais un type exécutable** (`text/javascript`, `application/javascript`, JSON de module, `application/wasm`, `text/html` hors affichage du lecteur). Une liste blanche fixe ce qui sort tel quel : images matricielles, CSS, polices, XHTML et SVG pour le lecteur ; tout le reste devient `application/octet-stream`. Le type est déduit de l'**extension contrôlée par le serveur**, jamais du manifeste de l'EPUB. Avec `nosniff`, un `<script src="/content/<ticket>/x.js">` ne s'exécute pas. `Content-Disposition: attachment` s'applique à SVG et HTML **sans exception** pour les réponses directes ; le lecteur ne les affiche qu'après lecture par `fetch` et passage en `blob:` (le plan L05, qui disait « hors iframe du lecteur », est aligné).

**Jamais l'original.** L'invariant I35 de l'ADR-0047 s'étend à `/content` : pour un EPUB `untrusted`, un ticket se résout **toujours vers le dérivé neutralisé** ; en son absence, vers un refus typé, **jamais vers l'original** gardé dans `/store`. Test : `test_content_never_serves_untrusted_original` (L05).

#### Filtre de contenu (client) et neutraliseur (serveur), liste commune
Le filtre s'applique à **tout** EPUB, quel que soit son niveau de confiance. Il remplace le « DOMPurify côté client » de la décision d'origine de l'ADR-0012 : c'est un filtre maison **en liste blanche**, sans nouvelle dépendance (aucune autre dépendance sans ADR, plan M5 §2).

**Accroche, sans patcher le sous-module** : les événements prévus du `Loader` de foliate (`book.transformTarget`, `epub.js`) :
- événement `load` : `detail.allow = false` pour tout `detail.isScript` et pour tout type hors liste blanche (une ressource déclarée comme script par l'OPF n'est **jamais** chargée) ;
- événement `data` : filtrage de `detail.data` pour tout type XHTML, HTML, SVG ou CSS, **avant** `URL.createObjectURL`, et après la réécriture d'URL de foliate, pour voir les URL finales.

**Type des `blob:`** : foliate crée chaque `blob:` avec le `mediaType` de l'OPF. Le filtre **fixe lui-même `detail.type`**, à partir d'une liste blanche (XHTML, HTML, SVG, CSS, images matricielles, polices) ; tout le reste devient `application/octet-stream`. Le type déclaré par l'OPF n'est jamais utilisé.

**Analyse** : `DOMParser` sur ce type final. En cas d'erreur d'analyse, le document est **refusé** (jamais réanalysé en HTML, ce que foliate ferait sinon), puis resérialisé après filtrage.

**Éléments retirés avec leur contenu, et remplacés dans le corps par un élément inerte à la même place** (voir « Stabilité du CFI ») : `script`, `iframe`, `frame`, `frameset`, `object`, `embed`, `applet`, `form`, `input`, `button`, `select`, `textarea`, `meta` (tout `http-equiv`, pas seulement `refresh`), `base`, `link` sauf `rel="stylesheet"` vers une entrée du livre, `portal` ; en SVG `script`, `foreignObject`, `animate`, `set`, `animateMotion`, `animateTransform` et `use` vers une ressource externe ; en MathML `maction` et `semantics`/`annotation-xml` à contenu HTML.

**Attributs retirés** : tout `on*`, `srcdoc`, `formaction`, `ping`, `http-equiv` ; tout attribut d'URL (`href`, `xlink:href`, `src`, `srcset`, `poster`, `data`, `action`, `background`, `cite`) dont la valeur n'est ni un `blob:` produit par le loader, ni un fragment `#…`, ni, **pour `a[href]` seulement** (le seul lien que foliate intercepte), un lien `https:` ou `http:` destiné à l'interception ; **`xlink:href` n'admet que `blob:` du loader et `#…`** ; MathML `href`.

**URL vers l'origine de l'instance** : toute URL absolue ou relative à la racine vers l'origine de l'instance est retirée, y compris dans `url()` de CSS (sinon un livre enverrait des `GET` authentifiés vers l'API).

**CSS** : `@import` et `url()` hors `blob:` du loader retirés ; `expression()` et `behavior` retirés.

**Liens externes** : l'adaptateur **annule toujours** l'événement `external-link` de foliate (qui ouvrirait sinon l'adresse sans `noopener`), demande une **confirmation**, puis ouvre l'adresse **hors du lecteur** en `noopener noreferrer`. Foliate n'intercepte que `a[href]` : un `<a xlink:href>` SVG est retiré par le filtre. Les liens internes sont résolus par foliate.

**Neutraliseur serveur** : `xhtml-neutralizer` passe de `@v1` à `@v2` avec cette même liste. Les dérivés existants sont régénérés par une **migration nommée** (ENF-OPS-021 : une nouvelle version ne réécrit jamais rien d'elle-même) ; lot ferme : **L05** (migration et test `test_neutralizer_v2_migration_regenerates_derivatives`).

**Corpus d'EPUB piégés** : script en ligne, `on*`, `<script src>` absolu et relatif (dont `/content/…`), `<script type="module" src>` vers un asset de la SPA, `javascript:` dans un lien et dans `xlink:href`, `meta refresh` et autre `http-equiv`, `<base>`, `<link rel="prefetch">`, `@import` et `url()` distants, `url()` absolu vers l'instance, SVG avec `<script>` et avec `<set attributeName="href" to="javascript:…">`, `<iframe srcdoc>`, iframe `blob:` imbriquée contenant un script, `<object data>` et `<embed>`, formulaire, MathML `href`, `data:text/html`, OPF qui déclare un `.js` ou une image en XHTML, XHTML invalide qui serait rebasculé en HTML, EPUB `publisher` avec `<script src="/content/…">`.

**Tests** : `test_epub_script_never_runs` (trois navigateurs), `test_content_never_serves_executable_type`, `test_external_link_never_navigates_iframe` (dont le cas SVG), `test_content_filter_whitelist_corpus` (L11, L12, L05).


#### Crédential de `/content`
- Admis : la **session** de l'utilisateur **et** le **ticket** de contenu (`/content/{ticket}/{path}`), lié au profil, au support, à la portée, à la liste d'entrées (extrait) et à une **empreinte de session** revérifiée côté serveur, de courte durée. Les **clés d'API et les jetons d'appareil sont refusés** sur `/content` (D11 ne disait que l'inverse).
- Le ticket est un porteur dans le chemin : `Referrer-Policy: no-referrer`, **absent des journaux d'accès et de l'application**.
- `Cache-Control: private` ; l'autorisation est évaluée **avant** l'`ETag` (ENF-SEC-010).
- La visibilité du profil est revérifiée à **chaque requête** : la requête suivante est refusée ; le lecteur ouvert affiche une erreur typée ; les copies hors ligne sont évincées à la reconnexion (L16b).
- Le service worker de la PWA (L16a) **exclut `/content/*` de son cache** : le contenu n'y entre que par la copie hors ligne de L16b, par profil, avec les en-têtes d'origine conservés.
- Tests : attaques enfant, membre, clé d'API, jeton d'appareil et IDOR sur la ressource d'autrui (L05).

#### Origine séparée (option)
Réglage de déploiement `SHELFHOLD_CONTENT_ORIGIN` (variable d'environnement : ce n'est **pas** une action réservée, ni dans la table d'ENF-SEC-010 ni dans l'énumération d'EF-USR-024, tant qu'il n'est pas modifiable à l'exécution), documenté avec compose et étapes à la charge du porteur (DNS, TLS, proxy).
- **Ce qu'elle apporte** : le cookie de session n'est pas envoyé à cette origine ; la navigation directe vers un contenu est isolée par l'origine elle-même.
- **Ce qu'elle n'apporte pas** : le rendu dans le lecteur. Les documents `blob:` prennent l'origine de la SPA, quelle que soit l'origine qui a servi les octets.
- **Règles à écrire pour elle** : CORS (`Access-Control-Allow-Origin` limité à l'origine de la SPA, sans `Allow-Credentials`) pour que la SPA puisse `fetch` ; l'empreinte de session du ticket est la seule liaison à la session (le cookie n'est pas envoyé).

### Accessibilité du lecteur (ENF-A11Y-002, volet M5)
- **Clavier** : foliate relaie les événements clavier de l'iframe (rendu possible par `allow-scripts`) ; le lecteur les transmet à la fenêtre hôte et applique ses raccourcis (`postMessage` entre le lecteur et son iframe quand un moteur en a besoin). Le focus est **toujours sortable** : une touche dédiée (Échap) rend le focus à la barre du lecteur ; pas de piège au clavier.
- **Ordre de tabulation** : barre du lecteur, puis contenu (iframe ou liste d'images), puis commandes de pagination.
- **`lang` et `dir`** du document du livre posés depuis la publication (RTL compris).
- **Page d'images** : texte alternatif « Page 12 sur 180 », sans le contenu de l'image.
- **Région live** : le changement de page est annoncé.
- Test : `test_keyboard_shortcuts_reader` exécuté **focus dans l'iframe**, sur Chromium, Firefox et WebKit (L13, L24).

### Port `ReaderEngine`
Un seul port côté front (`frontend/src/lib/reader/`) que le lecteur utilise ; deux moteurs :
- **moteur d'images maison** : manga paginé (LTR et RTL, doubles pages) et webtoon vertical (liste d'images, `IntersectionObserver`, préchargement) ; il décode par `<img>` ou `createImageBitmap`. Le worker sans réseau (02 §5.8) sert **côté serveur** aux dimensions et au manifeste ;
- **moteur EPUB sur foliate-js** (reflow et fixed-layout) : sous-module git épinglé par commit, jamais un paquet npm, **importé par son seul adaptateur** (règle ESLint `no-restricted-imports`, posée par L00).

Le port expose : ouvrir une publication, rendre une page ou un segment, produire et restaurer un locator (format ci-dessous), annoncer et relayer les raccourcis clavier, libérer ses ressources. Un moteur déclare les types de média qu'il lit **et ses garde-fous dans un champ typé** (`guards`) ; le **registre** choisit le moteur par type de média et refuse la construction si les garde-fous exigés pour ce type manquent. Le lecteur lit le **dérivé neutralisé** d'un EPUB `untrusted` (ADR-0047) ; un EPUB `publisher` aussi, par le filtre de contenu (liste blanche, aucun script).

### Format du locator stocké (figé pour L07)
JSON versionné, colonne `locator` de `reading_position` :
- **EPUB** : `{"v": 1, "type": "cfi", "cfi": "epubcfi(…)", "totalProgression": 0.37, "filter": "content-filter@2"}` ; `cfi`, `totalProgression` (0 à 1) et `filter` (version du filtre et du neutraliseur qui ont produit le DOM) sont **obligatoires**. Aucune liste de positions Readium n'est supposée : foliate n'en calcule pas.
- **CBZ, PDF et tout support de pages** : `{"v": 1, "type": "page", "index": 11, "count": 180}` ; `index` est un **index de page à partir de 0**, `count` le nombre de pages du support ; `totalProgression = (index + 1) / count`.

**Stabilité du CFI (N2, réglé avant L07).** Un CFI compte les éléments frères et les nœuds de texte : retirer un élément du corps décalerait toutes les positions. Le filtre client **et** le neutraliseur **remplacent** donc chaque élément retiré du corps par un **élément inerte à la même place** (élément vide, sans attribut, de même rang) : la structure du DOM est préservée, les étapes de CFI restent stables d'une version à l'autre et entre filtre client et neutraliseur. Une position écrite sous une autre version de `filter` est **restaurée puis marquée `approximate`** (invariant I30) ou signalée, **jamais déplacée en silence**. La migration `@v1` vers `@v2` du neutraliseur a son test nommé, `test_neutralizer_v2_migration_regenerates_derivatives`, et un lot ferme : **L05**, dont la fiche du plan reçoit la migration (boîte à revoir).

Tests : `test_content_filter_preserves_cfi_steps` (propriété, L11 et L12), `test_locator_restored_across_filter_versions` (L07), `test_locator_v1_schema_rejects_missing_fields` (L07) : refus d'un EPUB sans `cfi`, sans `totalProgression` ou sans `filter`, et d'un support de pages dont `index ≥ count`.

### D6 — kosync : l'ADR-0053 amendée prime
01 (EF-LEC-009) et 03 sont alignés sur l'ADR-0053 amendée le 2026-11-16 :
- **`200` à tout `PUT`**, y compris pour une empreinte inconnue, cachée, d'un autre profil, ambiguë ou une garde d'intégrité qui refuse ; le motif réel va au journal et à un compteur par jeton ; seul un corps mal formé reste un `422` (ou un `413`) ; **un crédential invalide reste `401`** (ADR-0053) ;
- **aucune devinette** : le champ `metadata` est reçu et ignoré, aucun appariement par titre, taille ou métadonnée ; un hash tenu par deux supports ne se résout pas ;
- **`GET`** d'un document non résolu : `200 {}` ;
- **aucun `409` en HTTP** ;
- `x-auth-key` est le md5 du **secret du jeton d'appareil** (jamais du mot de passe du compte) ; 02 est corrigé ;
- les deux tests de 03 sont **renommés** : `test_kosync_metadata_fallback_match` devient `test_kosync_metadata_never_used_to_match` et `test_kosync_ambiguous_hash_409` devient `test_kosync_ambiguous_hash_never_guesses`.

### D7 — Aucun moteur PDF en M5
EF-LEC-004 reste « Plus tard ». Le port n'enregistre **aucun moteur PDF** en M5. Un moteur déclare ses garde-fous par un champ typé (`guards: { isEvalSupported: false }`) que le registre **exige pour `application/pdf`** ; le registre ne peut pas inspecter les options réellement passées à pdf.js, il vérifie le contrat déclaré. `test_pdfjs_is_eval_supported_false` (ENF-SEC-004, CVE-2024-4367) prouve deux choses : aucun moteur PDF n'est enregistré ; un moteur PDF qui s'enregistrerait sans ce garde-fou déclaré fait **échouer la construction du registre**.

### D8 — Seuil de conflit des positions (06 Q47, tranchée)
Le principe de G10 est conservé : `client_ts` le plus récent, borné par `server_ts`, historique conservé (EF-LEC-007). Un **conflit n'est proposé** que si la position perdante est **plus avancée** que la gagnante de plus de **2 % de progression** :
- **EPUB** : écart de `totalProgression` supérieur à 0,02 ; la borne de « 3 pages » ne s'applique pas (en reflow, le nombre de pages dépend de la fenêtre) ;
- **supports de pages** : écart supérieur à 2 % **et** d'au moins 3 pages.

Une seule proposition par (profil, support) jusqu'au choix de l'utilisateur ; en dessous du seuil, fusion silencieuse avec historique. Valeurs par défaut, réglables.

### D9 — Dates dans 01
- **ENF-A11Y-002** est datée « M3 (atelier) / M5 (lecteur) » : son texte nomme déjà le lecteur de M5.
- **EF-EXP-058** porte son volet M5 : la liaison `pull` (OPDS, kosync Shelfhold) ne sert que depuis un exemplaire lisible (`/store` ou copie d'arbre). Elle est datée « M1a / M5 (volet `pull`) ».
- Les avertissements de `traceability.py --milestone M5` (« listée par 03 pour M5, mais 01 donne… ») restent acceptés pour les autres volets, comme en M2 à M4.

## Conséquences
- **Positives** : le lecteur tient la définition de fini sans concurrencer Kavita ; l'isolation par défaut ne demande rien au porteur ; la sync des liseuses reste sûre ; les contradictions de 01, 02 et 03 sont fermées avant que du code en dépende.
- **Négatives** : deux moteurs à maintenir (images et EPUB) ; foliate-js n'a pas de release stable (R13) ; foliate-js impose `allow-same-origin allow-scripts` à son iframe : l'isolation de l'EPUB repose sur la CSP héritée par le `blob:`, le neutraliseur et les filtres, pas sur un sandbox d'iframe ; la CSP du lecteur élargit `style-src` (limité à l'entrée `/lire`) ; l'absence d'exécution de script tient sur deux barrières (CSP héritée, filtre), et un navigateur sous la version minimale n'a pas le moteur EPUB.
- **Aucune migration** dans ce lot : documentation seule.

## Risque principal
L'isolation du contenu. Mitigation : une seule sortie (`content_response`), tests d'attaque sur le corpus d'EPUB piégés (enfant, membre, clé d'API, IDOR sur la ressource d'autrui), CSP du lecteur testée directive par directive, filtre de type de `/content`, filtre de contenu en liste blanche indépendant de la CSP, trois navigateurs dont WebKit. L'origine séparée n'est pas une mitigation du rendu (voir D-P2).

## Réversibilité
Bonne pour le moteur (derrière `ReaderEngine`) et pour l'origine (un réglage). Moyenne pour le rôle du lecteur (D-P1) : élargir au catalogue entier est un ajout de routes, pas une refonte, mais rouvre Q22.

## Amendements et corrections portés par cette ADR
- **ADR-0012** : rôle selon D-P1, chemin `/content/*` par défaut (D-P2), modèle de rendu (l'iframe sans `allow-scripts` de la passe 1 est abandonnée pour l'EPUB), port `ReaderEngine`, aucun moteur PDF en M5 (D7). Le texte d'origine de pdf.js « livré avec le lecteur » est suspendu.
- **ADR-0018** : OIDC et registre d'appareils en M5 ; jetons d'appareil livrés en M4 par l'ADR-0053 ; `x-auth-key` = md5 du secret du jeton.
- **ADR-0053** : l'amendement du 2026-11-16 prime sur 01 et 03 (D6) ; passage d'EPUB repris en M5 (L09).
- **01** : EF-LEC-009 (D6), ENF-A11Y-002 et EF-EXP-058 (D9). **02** : `x-auth-key` (route kosync), index des ADR (« OIDC en V2 » devient « OIDC en M5 »). **03** : tests kosync renommés, D7, D8, D-P2. **04** : module `reading/`. **06** : Q22 et Q47 « tranchées ».

## Exigences liées
EF-LEC-001, EF-LEC-002, EF-LEC-003, EF-LEC-005, EF-LEC-006, EF-LEC-007, EF-LEC-009, EF-LEC-014, EF-EXP-058, ENF-SEC-004, ENF-A11Y-002.

## Sources
- ADR-0012, ADR-0018, ADR-0020, ADR-0027, ADR-0028, ADR-0045, ADR-0047, ADR-0053 (et son amendement du 2026-11-16)
- `docs/spec/06-points-discutables.md` (Q22, Q47)
- https://github.com/johnfactotum/foliate-js
- https://github.com/advisories/GHSA-wgrm-67xf-hhpq (pdf.js, CVE-2024-4367)

## Acceptation (2026-11-18)
Acceptée (décision déléguée au mainteneur, plan M5 D-P1/D-P2), après relecture adverse (section « #345 : contradicteur » de `docs/decisions/M5.md`) dont les constats sont corrigés ici. D6 à D9 sont des décisions déléguées révisables par écrit.
