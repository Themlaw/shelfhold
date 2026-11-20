# Dépendances critiques

Exigence ENF-GOV-002 (risque R21, ADR-0026). Une dépendance est **critique** quand son abandon
ou son changement de licence obligerait à réécrire une fonction du produit. Chacune vit derrière
**un adaptateur dédié** : le contrat import-linter `critical-*` de `backend/importlinter.ini`
interdit tout import hors de l'adaptateur. Le **facteur bus** est la part des contributions du
premier contributeur (API GitHub `contributors`, 100 premiers, relevé du 2026-11-04) ; la
colonne « Dernière release » donne le dernier tag publié à la même date.

Revue trimestrielle, assistée par Renovate (ENF-GOV-003). Un signal de déclenchement atteint
ouvre une issue `dependency-risk` et fait basculer vers le repli décrit.

| Dépendance | Rôle | Facteur bus | Licence | Dernière release | Repli | Signal de déclenchement | Adaptateur |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Huey | File de jobs SQLite (`SqliteHuey`, ADR-0005) | 92 % (coleifer) | MIT | 3.5.4 (2026-11-01) | Runner maison minimal sur SQLite, seulement après un nouvel ADR | 12 mois sans commit, changement de licence, dépôt archivé | `shelfhold/jobs/huey_app.py` (contrat `critical-huey`) |
| foliate-js | Moteur du lecteur intégré EPUB (M5) | 90 % (johnfactotum) | MIT | aucune release, sous-module épinglé sur un commit (dernier commit 2026-05-01) | Moteur EPUB alternatif derrière `ReaderEngine`, ou lecture déléguée à Kavita | 12 mois sans commit, changement de licence, dépôt archivé | Interface front `ReaderEngine` (`frontend/`, M5) |
| @vite-pwa/sveltekit | Coquille PWA installable : construit `src/service-worker.ts` avec la liste des fichiers du build (M5 L16a, EF-LEC-012, ADR-0013, D14) | 94 % (antfu, 129 contributions sur 137, API `contributors`, relevé du 2026-11-19) | MIT | 2.0.3 (2026-11-17) | Service worker écrit à la main et liste des fichiers du build lue par un script : le worker (`src/lib/pwa/worker.ts`) n'importe rien du paquet | 12 mois sans release, changement de licence, dépôt archivé | `frontend/vite.config.ts` seul (devDependency de build, rien de lui n'est livré au navigateur) |
| lncrawl | Source de romans via le méta-adaptateur (plugin natif, M2) | 80 % (dipu-bd) | GPL-3.0 | v4.14.0 (2026-08-03) | Sources natives Python écrites au cas par cas | 12 mois sans commit, changement de licence, dépôt archivé | `shelfhold/plugin_host/adapters/meta/lncrawl` |
| Kavita | Destination de l'export, service tiers et non une bibliothèque Python (M1a) | 71 % (majora2007) | GPL-3.0 | v0.9.1.4 (2026-09-02) | Destination `tree` (arborescence de fichiers) lisible par tout lecteur de bibliothèque | Rupture de l'API sur une route de la liste (`target.route_missing`), changement de licence, archivage | Adaptateur de destination Kavita (`shelfhold/export`, M1a) |
| kepubify | Conversion EPUB vers KEPUB, binaire externe (plus tard) | 99 % (pgaskin) | MIT | v4.0.4 (2022-03-09) | Étape de conversion désactivée ; export EPUB seul | 12 mois sans commit (déjà atteint : binaire figé, usage optionnel), changement de licence, archivage | Adaptateur de conversion (`shelfhold/export`, plus tard) |
| Suwayomi | Source connectée (serveur de téléchargement CBZ, M1a) | 43 % (AriaMoradi) | MPL-2.0 | v2.4.2366 (2026-09-30) | Sources natives (`plugin_host/adapters/native`) ; contrat S5 hebdomadaire pour détecter la rupture | Échec du contrat S5, retrait de la route de téléchargement REST sans équivalent GraphQL, changement de licence, archivage | `shelfhold/plugin_host/adapters/meta/suwayomi` |
| comicbox | Lecture et écriture de ComicInfo, si retenu (M1a) | 99 % (ajslater) | LGPL-3.0 | v5.5.1 (2026-11-02) | Génération ComicInfo.xml par `defusedxml` et un modèle interne | 12 mois sans commit, changement de licence, archivage | `shelfhold/library/adapters/comicinfo` (contrat `critical-comicbox`) |
| pykakasi | Romanisation des titres et noms japonais à l'indexation de la recherche (M3 L17, D6) | non relevé (dépôt personnel, miurahr) | GPL-3.0-or-later (compatible AGPL-3.0) | 2.3.0 | Titres japonais non romanisés : la recherche reste par écriture d'origine et par trigrammes | 12 mois sans commit, changement de licence, dépôt archivé | `shelfhold/catalog/application/search/romanize.py` (Python pur, dictionnaire dans la roue, aucun téléchargement à l'exécution) |

## Contrats d'import

- Les dépendances Python (Huey, comicbox) ont un contrat `forbidden` actif dès M0 : aucun module
  de `shelfhold` ne les importe, sauf l'adaptateur nommé ci-dessus. L'adaptateur est ajouté à
  `ignore_imports` du contrat le jour où il est écrit.
- Les services tiers (Kavita, Suwayomi), le binaire kepubify et le composant front foliate-js ne
  s'importent pas en Python ; leur confinement est vérifié par les contrats « aucun symbole
  `suwayomi` ou `lncrawl` hors de son adaptateur » (EF-SRC-033) à l'écriture de chaque adaptateur.
- FastAPI et Starlette sont confinés à `shelfhold.interfaces` (contrat
  `fastapi-only-in-interfaces`).

## foliate-js (sous-module)

Moteur EPUB du lecteur intégré (M5, ADR-0055, EF-LEC-003, risque R13). Le code de foliate-js n'est
ni copié ni installé par un gestionnaire de paquets : c'est un **sous-module Git**
(`frontend/vendor/foliate-js`, `.gitmodules`) épinglé par **commit**, jamais par branche ni par tag.

Commit épinglé : `78914aef4466eb960965702401634c2cb348e9b1`

- **Dépôt officiel** : `https://github.com/johnfactotum/foliate-js` (HTTPS, aucune autre URL
  admise). Commit vérifié sur le dépôt officiel le 2026-11-18 : tête de `main`, commit fusionné
  par l'interface GitHub (signature « vérifiée » : elle atteste le passage par GitHub, pas l'identité
  de l'auteur), date 2026-05-01 (« Use original hrefs for external links and add isExternal in fb2.js »),
  dépôt non archivé, licence détectée MIT.
- **Licence** : MIT (© John Factotum), relue dans le `LICENSE` du sous-module. Le code embarqué dans
  son `vendor/` garde ses licences : fflate (MIT), zip.js (BSD-3-Clause), SJCL embarquée dans `vendor/zip.js` (double licence BSD-2-Clause ou GPL-2.0 : **l'option BSD-2-Clause est retenue**), pdf.js (Apache-2.0, avec
  cmaps Adobe, polices Foxit et Liberation, SIL OFL 1.1). Les textes complets de fflate (MIT), de zip.js (BSD-3-Clause) et de SJCL (BSD-2-Clause) sont reproduits dans `THIRD_PARTY_NOTICES`, car les bundles minifiés n'ont ni en-tête ni fichier de licence voisin. pdf.js
  n'est enregistré par aucun moteur en M5 (ADR-0055, D7). MIT, BSD-3-Clause et Apache-2.0
  sont compatibles avec l'AGPL-3.0 (ADR-0014) ; les polices (SIL OFL 1.1) et les données de pdf.js
  sont de simples fichiers de données, agrégés et jamais chargés en M5 : l'image n'embarque pas
  `vendor/pdfjs` : `.dockerignore` l'exclut du contexte de construction (avec les tests, les workflows et
  la configuration de build du sous-module), l'image d'exécution ne reçoit que `frontend/build`, et
  `test_the_image_does_not_contain_pdfjs` le vérifie.
- **Aucun script exécuté depuis le sous-module** : son `package.json` (`build` : rollup) et ses
  dépendances de développement ne sont jamais installés ni lancés. Les bundles de son `vendor/` sont
  utilisés tels quels ; **chaque fichier de `vendor/`**, quelle que soit son extension, est épinglé par
  SHA-256 dans `tools/foliate-vendor.sha256` (fichier en trop, manquant ou modifié : échec) et balayé
  (aucun `@import`, `url()`, `import` ni `src` vers un hôte distant, CSS compris) ; leurs versions
  (zip.js 2.8.22, fflate 0.8.2, pdf.js 5.5.207) sont celles du `package-lock.json` de foliate :
  **déduites du verrou, non reproduites** (reproduire les bundles à partir des tarballs npm vérifiés
  par leur `integrity`, dans un bac à sable hors CI, est un travail laissé à la première mise à jour). `frontend/package.json` ne le référence pas (ni dépendance, ni espace de
  travail) ; lint, typos et vérificateur de types l'ignorent. `.gitmodules` n'a ni `branch` ni
  `update`.
- **Aucune requête réseau à l'exécution, aucune ressource d'un CDN** : les `fetch` de la
  bibliothèque servent à ouvrir une adresse (`view.js`, `makeBook` reçoit une chaîne), une couverture
  FB2 et les feuilles de pdf.js ; la sortie d'un lien externe est `globalThis.open` (`view.js`),
  annulée par l'adaptateur (ADR-0055). L'adaptateur ne passe jamais d'adresse, de `File` ni de `Blob`
  à `view.open` (voir l'audit). zip.js embarque du code de Web Worker, inerte tant que `view.js`
  appelle `configure({ useWebWorkers: false })` (vérifié dans le code, commentaires retirés par un analyseur lexical : tout autre appel `configure`, ou tout `useWebWorkers` autre que `false`, fait échouer le garde). Les seules
  adresses absolues du code sont des espaces de noms XML et des liens de commentaires.
  **La barrière d'exécution est la CSP du lecteur** (`connect-src 'self'`, `worker-src 'none'`,
  ADR-0055), pas un balayage. `tools/check_submodule_pin.py assets` (CI, job `frontend`, après
  checkout du sous-module) et `test_no_reader_asset_from_third_party_origin` sont une **alarme** :
  elles refusent un nouvel hôte, un script de `vendor/` hors de ceux épinglés, et tout nombre d'appels
  supérieur à l'inventaire audité (`fetch`, `open`, `import()` et les `import … from` statiques, `Worker`, `WebSocket`, `sendBeacon`,
  `.src =`, `srcdoc`, `location`, `eval`, `innerHTML`, déplacement de nœuds) ; elles ne comprennent
  pas du code construit dynamiquement.
- **CI** : les jobs qui construisent le frontend ou l'image récupèrent le sous-module avec
  `actions/checkout` (`submodules: true`, jamais `recursive`, `persist-credentials: false`) : le
  commit est celui du lien Git, donc l'arbre extrait est celui du SHA. `tools/check_submodule_pin.py pin`
  (job `lint`, toujours exécuté) échoue si le lien Git n'est pas le « Commit épinglé » ci-dessus (ni dans le tableau d'audit ni dans
  `THIRD_PARTY_NOTICES`), s'il existe un autre lien Git, ou si `.gitmodules` (lu comme Git le lit)
  n'a pas exactement un `path` et l'URL officielle. `test_foliate_submodule_pinned_to_documented_commit` le
  prouve.
- **Mises à jour** : Dependabot (`gitsubmodule`, mensuel, `cooldown` de 14 jours, jamais de
  fusion automatique) ouvre une proposition ; le commit documenté (trois endroits) et les notices sont
  imposés par `pin` : elle reste rouge tant qu'une personne ne les met pas à jour, avec la date de
  vérification. Dependabot propose la tête de `main` ; rien ne prouve que `cooldown` s'applique à
  l'écosystème `gitsubmodule` : l'âge d'au moins 14 jours est donc vérifié par la relecture.

### Règle de mise à jour

Une mise à jour de foliate-js est un changement de code non fiable dans le lecteur : elle est lente
et relue.

- **Qui** : le porteur du dépôt, seul. Jamais une fusion automatique, jamais un agent sans relecture
  d'un second regard. Une PR de Dependabot est une proposition, pas une autorisation.
- **Comment** : (1) choisir un **commit** de `main` du dépôt officiel, vérifié par GitHub, vieux d'au
  moins 14 jours ; (2) `git -C frontend/vendor/foliate-js fetch origin` puis
  `git -C frontend/vendor/foliate-js checkout <sha>` ; (3) dans le **même commit** : le lien Git, la
  ligne « Commit épinglé » et sa date de vérification, `THIRD_PARTY_NOTICES`, la section du jalon dans
  `docs/decisions/` ; (4) `python3 tools/check_submodule_pin.py pin --min-age 14` (l'âge du commit épinglé, lu dans le sous-module extrait) et `assets --require-checkout`,
  puis les suites du lecteur (corpus d'EPUB piégés sur les trois navigateurs, filtre de contenu, CSP du
  lecteur).
- **Relecture** : `git diff <ancien>..<nouveau>` du sous-module lu en entier pour les fichiers du
  tableau d'audit ci-dessous et pour `epub.js`, `view.js`, `paginator.js`, `fixed-layout.js` ; le
  tableau d'audit est refait (recherche de `cloneNode`, `importNode`, `adoptNode`, `extractContents`,
  `cloneContents`, `insertNode`, `innerHTML`, `srcdoc`, `eval`) ; `LICENSE` et `vendor/` relus (licence,
  versions, notices) ; toute nouvelle primitive réseau ou tout nouvel hôte est refusé. Une relecture
  adverse indépendante est exigée dès qu'un fichier du tableau change.

## foliate-js : audit des chemins qui déplacent des nœuds

Condition de `'strict-dynamic'` (ADR-0055) : sous cette CSP, un script inséré par l'analyseur est
refusé, mais un nœud `<script>` que du code de confiance clone, importe ou rattache à un document
vivant s'exécuterait. L'audit porte sur le commit `78914aef4466eb960965702401634c2cb348e9b1`, relu le 2026-11-18 : chaque appel qui
clone, importe, extrait, insère ou réinsère un nœud, et chaque `innerHTML`.

| Fichier | Chemin relevé | Verdict |
| --- | --- | --- |
| `view.js` | `makeBook` choisit le format **sur le contenu ou le nom** d'un `File`, d'un `Blob` ou d'une chaîne (`%PDF-` → `pdf.js`, octets MOBI → `mobi.js`, `.fb2`, `.fbz`, `.cbz` → `fb2.js`, `comic-book.js`) ; ces chemins créent leurs `blob:` **sans l'événement `data`** : le filtre ne s'y applique pas. `fetch(url)` si on lui donne une adresse ; `globalThis.open(href, '_blank')` (liens externes, ligne 361) ; `initTTS` et la recherche en import dynamique. Aucun nœud du livre déplacé. | **écarté** : l'adaptateur ne passe **jamais** de `File`, `Blob` ni de chaîne à `view.open` (voir la consigne pour L12) ; il construit l'objet livre EPUB lui-même. |
| `epub.js` | Le chargeur analyse chaque XHTML, HTML ou SVG par `DOMParser`, réécrit des attributs, remplace des instructions de traitement (`replaceWith`, document détaché), sérialise en chaîne puis crée un `blob:` ; l'événement `data` est le point d'accroche du filtre. | inerte : le document analysé n'est jamais rattaché ; seul le texte filtré devient un `blob:`. |
| `epubcfi.js`, `progress.js`, `text-walker.js`, `uri-template.js` | Calcul de CFI, de progression, parcours de texte, gabarits d'URL : lecture de `Range` et de chaînes. | sans déplacement : aucun nœud créé ni déplacé. |
| `paginator.js` | Ajoute deux `<style>` créés par `doc.createElement` dans le `<head>` ; `replaceChildren` sur l'en-tête et le pied de l'hôte ; `innerHTML` d'un gabarit statique dans son propre shadow root ; pose `sandbox="allow-same-origin allow-scripts"` sur son iframe. | sans déplacement : aucun nœud du livre n'est déplacé (les `<style>` ajoutés sont créés par foliate). |
| `fixed-layout.js` | Crée ses iframes et ses éléments ; `replaceChildren` sur sa propre racine. | sans déplacement : aucun nœud du livre. |
| `overlayer.js` | Crée des éléments SVG avec le `document` hôte ; ne lit du livre que `range.getClientRects()`. | sans déplacement : aucun nœud du livre cloné ni déplacé. |
| `search.js` | `textWalker` et des `Range` : produit des chaînes et des CFI. `view.js` lit les sections par `createDocument()`, soit `DOMParser` sur le texte brut (scripts désactivés, jamais rattaché). | inerte : documents détachés, jamais insérés. La recherche plein texte n'est pas branchée en M5. |
| `footnotes.js` | `FootnoteHandler.#showFragment` : une seconde `<foliate-view>` du même livre, puis `range.extractContents()` et `appendChild` dans le même document. Les nœuds du livre sont déplacés par du code de foliate. | **écarté** : `footnotes.js` n'est pas importé en M5 (condition posée à L12, qui la fait tenir par ESLint). Les notes passent par un nouveau document `blob:` filtré. |
| `tts.js` | `range.cloneContents()`, `range.insertNode(mark)` sur le fragment cloné, `importNode` vers un document XML SSML détaché. | **écarté** : `view.initTTS` n'est jamais appelé en M5 (import dynamique) ; les clones restent dans des fragments détachés. |
| `pdf.js` | Charge pdf.js (`vendor/pdfjs`), `fetch` des feuilles de style, `adoptNode` ; le calque d'annotations de pdf.js construit du DOM vivant à partir du PDF. | **écarté** : aucun moteur PDF en M5 (D7) ; inatteignable tant que `view.open` ne reçoit pas de fichier. |
| `mobi.js` | `innerHTML` sur un `<textarea>` hôte (ligne 4), `DOMParser`, `blob:` sans événement `data`. | **écarté** : format non enregistré en M5 ; inatteignable tant que `view.open` ne reçoit pas de fichier. |
| `fb2.js` | `DOMParser`, `innerHTML` lu pour l'annotation, `outerHTML` sérialisé dans un `blob:` sans événement `data`, `fetch` de couverture. | **écarté** : format non enregistré en M5 ; inatteignable tant que `view.open` ne reçoit pas de fichier. |
| `comic-book.js` | Construit par concaténation une page HTML `<img src="blob:…">` en `blob:`, sans événement `data`. | **écarté** : le moteur d'images de L10 ne l'utilise pas ; inatteignable tant que `view.open` ne reçoit pas de fichier. |
| `quote-image.js` | `innerHTML` et `importNode` pour fabriquer une image de citation (`data:image/svg+xml`). | **écarté** : non importé en M5. |
| `opds.js` | `innerHTML` lu sur un élément d'un flux analysé ; import dynamique de `uri-template.js`. | **écarté** : non importé en M5 (le catalogue OPDS est celui de Shelfhold). |
| `dict.js` | Lecture de dictionnaires dictd et StarDict : octets et chaînes. | **écarté** : non importé en M5 ; sans déplacement de nœuds. |
| `reader.js`, `reader.html` | Démonstration de foliate : `open`, `location`, `import()`, `new URLSearchParams(location.search)`. | **écarté** : non importés, jamais construits ni servis. |
| `ui/menu.js`, `ui/tree.js` | Éléments créés par le `document` hôte (`append`, `replaceChildren`) pour un menu et une table des matières. | **écarté** : non importés en M5 (l'interface du lecteur est celle de Shelfhold). |

Faits relevés, hors verdict par fichier : seuls `view.js` (`makeBook`), `fb2.js` et `pdf.js` font un
`fetch` ; la sortie d'un lien externe est `globalThis.open` (`view.js`, `external-link` annulé par
l'adaptateur, ADR-0055) ; le `Loader` de `epub.js` est le seul chemin qui passe par l'événement `data`.

Conséquence pour L12, conditions de `'strict-dynamic'` et de l'absence de filtre sur les formats
hors EPUB :
- **ne jamais passer de `File`, de `Blob` ni de chaîne à `view.open`** : construire l'objet livre dans
  l'adaptateur (`new EPUB(loader).init()` après avoir posé les gestionnaires `load` et `data` du
  `Loader`) et passer cet objet ; sinon un PDF, un MOBI ou un FB2 déposé comme EPUB serait rendu par
  un chemin sans filtre ;
- ne pas importer `footnotes.js`, `tts.js`, `pdf.js`, `mobi.js`, `fb2.js`, `comic-book.js`,
  `quote-image.js`, `opds.js`, `dict.js`, `reader.js` ni `ui/` ; ne pas appeler `initTTS` ;
- ne cloner, importer ni réinsérer aucun nœud du livre ;
- un test de build échoue si le bundle du lecteur contient un morceau `pdf`, `mobi`, `fb2` ou
  `comic-book` (Vite crée un morceau par `import()` de `view.js` : il faut l'éviter en important
  `epub.js`, `paginator.js`, `fixed-layout.js` et `overlayer.js` directement, ou le prouver) ;
- ces règles sont cadrées par ESLint et par le corpus de livres piégés (cas « script déplacé par une
  API DOM », cas « PDF déposé comme EPUB »).

Un chemin nouveau, issu d'une mise à jour, est écarté ou passe par le filtre avant la fusion.

**Mise en œuvre (L12).** `frontend/src/lib/reader/engines/foliate.ts` est le seul importeur (chemin relatif vers `frontend/vendor/foliate-js/epub.js` et `view.js`, jamais un pointeur dans `package.json`) ; il construit l'objet livre et passe cet objet. `frontend/vite.reader.config.ts` remplace par un module qui lève les `import()` de `view.js` vers `pdf`, `mobi`, `fb2`, `comic-book`, `tts`, `search`, `footnotes`, `quote-image`, `opds`, `dict`, `reader`, `ui/` et `vendor/{pdfjs,fflate,zip}` ; `frontend/scripts/reader-build.test.ts` échoue si le build du lecteur contient un morceau ou une marque de l'un d'eux. ESLint refuse tout import de foliate hors de l'adaptateur (statique, `import()`, chemin relatif).

## @vite-pwa/sveltekit (PWA, M5 L16a)

Exception prévue par PLAN-M5 §2 (D14), justifiée ici : plancher `^2.0.3`, **devDependency de build**.

- **Licence** : MIT (le paquet et ses 13 dépendances transitives nouvelles : `@unplugin-pwa/{core,vite,inspector}` 0.0.7,
  `@vite-pwa/workbox-{build,swkit,window}` 8.0.15, `verkit`, `hookable`, `valibot`, `proper-tags` en MIT ;
  `idb` en ISC ; `serialize-javascript` en BSD-3-Clause ; `@types/trusted-types` en MIT). Toutes compatibles AGPL-3.0.
- **Maintenance** : dépôt `vite-pwa/sveltekit` actif (dernier commit 2026-11-18, non archivé) ; la v2.0.0 (2026-11-01)
  est la refonte pour SvelteKit 3 (`withPwa`), suivie de trois correctifs en seize jours. Facteur bus élevé (un auteur
  fait 94 % des contributions) : c'est la raison du repli ci-dessus, et du choix de ne lui demander **que** la
  construction du worker.
- **Ce qui est livré au navigateur** : rien du paquet. Le worker est `frontend/src/service-worker.ts` et
  `frontend/src/lib/pwa/worker.ts` (sans Workbox, sans `eval`, sans `import()`, sans adresse distante : un test de
  source le vérifie) ; le paquet ne fait que remplacer `self.__WB_MANIFEST` par la liste des fichiers du build.
- **Ce qui est désactivé** : l'enregistrement en ligne (`injectRegister: false`, et `serviceWorker.register: false` côté
  SvelteKit : l'un et l'autre ajouteraient un script en ligne à une page dont la politique est par hachage ; le SPA
  s'enregistre depuis un module de l'origine) et la génération du manifeste (`manifest: false` : c'est le fichier
  `static/manifest.webmanifest`).
- **Avertissement `globStrict`** : le plugin ajoute `prerendered/**` à la liste des fichiers ; une SPA n'en a pas, donc
  `globStrict: false` (seul cet avertissement est tu, les autres motifs restent stricts par leur contenu).

## Autres dépendances notables

Non critiques (remplaçables sans réécriture), listées pour mémoire :

- **qrcode-generator** (MIT, 2.0.4, aucune dépendance) : dessine en local le QR de « Connecter un
  appareil » (M3 L48, EF-UX-004). Il ne code que des adresses de Kavita, jamais une clé ni un
  jeton, et n'ouvre aucune connexion : le composant `QrCode.svelte` ne contient aucun appel réseau
  (test `frontend/src/routes/appareils/page.test.ts`). Repli : un encodeur maison (version 1 à 10,
  correction L) ou l'adresse écrite seule, déjà affichée à côté de chaque code.
- **httpx2** (BSD-3-Clause) : client HTTP sortant du fetcher (M1a) et du `TestClient` de
  Starlette, qui déprécie `httpx` ; repli : `httpx`. Le domaine ne l'importe jamais (contrat
  `domain-pure`).
- **argon2-cffi** (MIT) : hachage `argon2id` des mots de passe. **pynacl** (Apache-2.0) :
  XChaCha20-Poly1305 pour les secrets et les pierres tombales. **pyrage** (MIT/Apache-2.0) :
  flux age x25519 du corps d'une sauvegarde. L'enveloppe age/scrypt de la phrase secrète (export du
  trousseau, clé éphémère d'une sauvegarde) est écrite et lue par `secrets/age_scrypt.py` avec
  pynacl (ChaCha20-Poly1305) et la bibliothèque standard (scrypt, HKDF, HMAC) : aucune dépendance
  ajoutée, et un facteur scrypt fixe (pyrage le règle d'après une mesure de la machine, ce qui rend
  un fichier parfois illisible sous charge). Repli commun : `cryptography`.
- **defusedxml** (PSF-2.0) : seule porte d'entrée XML (`infrastructure/xml`, ENF-SEC-014) ; repli :
  lxml durci. **nh3** (MIT) : assainissement du HTML amont, derrière `sanitize_upstream_text`
  (ENF-SEC-015) ; repli : `bleach`. Les contrats `xml-single-entry` et `sanitize-single-entry`
  interdisent l'import de `xml`, `lxml`, `nh3` et `bleach` ailleurs que dans leur porte d'entrée.
- **apprise** (BSD-2-Clause) : notifications ; planchers de sécurité ENF-SEC-016.
  **Pillow** (MIT-CMU) et **python-multipart** (Apache-2.0) : planchers ENF-SEC-016, vérifiés dans
  le verrou par `test_dependency_floors_enforced_in_lockfile`.
- **JPEG XL et HEIC/HEIF : non embarqués** (ADR-0054). `pillow-jxl-plugin` (≈ 2,5 Mo, GPL-3.0 dans
  son dépôt, compatible AGPL) et `pillow-heif` (BSD-3, ≈ 6 Mo, libheif et libde265 sous LGPL, brevets HEVC)
  ajouteraient une surface native au worker de décodage, qui tourne sans threads, et le verrou est
  figé. Une page dans l'un de ces formats est refusée avec le code `page.format_unsupported`.
  Un lot suivant est possible pour JPEG XL (un seul fil, essai seccomp, relecture adverse).
  L'AVIF, lui, est lu par Pillow ≥ 13.
  Les rendus d'images (M5 L20a, EF-BIB-015) s'appuient sur ce seul décodeur : AVIF et WebP vers JPEG ou PNG ; un JPEG XL est refusé (`rendition.format_unsupported`). Aucune dépendance ajoutée.
- **pypdf** (BSD-3-Clause) : lecture stricte d'un PDF du dossier d'import (EF-SRC-012), version
  exacte `6.21.4` (publiée le 2026-10-28, plus de 14 jours). Importée par le seul worker de décodage
  (contrat `pypdf-confined`), donc dans un sous-processus borné en mémoire et en temps ; un PDF
  n'est rangé que si elle l'ouvre sans erreur, sans chiffrement, sans fichier joint.
- **resvg-py** (MPL-2.0, liaison Python de resvg) : rastérise les pages SVG d'un CBZ `untrusted`
  en PNG sans perte (EF-BIB-051 (6), M2 L10, D10). **Seule exception au gel de `uv.lock`** : version
  exacte `==0.5.0`, publiée le 2026-08-24 (plus de 14 jours, donc dans la fenêtre `exclude-newer`),
  roues binaires pour toutes les plateformes visées, sans autre dépendance. Justification : un
  moteur sans script, sans réseau et sans accès au système de fichiers par construction (resvg ne
  charge une ressource que depuis le répertoire qu'on lui donne, et on ne lui en donne aucun ni
  aucune police système), plutôt que cairosvg (bibliothèque C cairo, fait des requêtes par défaut).
  Importée par le seul worker de décodage (contrat `resvg-confined`), donc dans un sous-processus
  borné en mémoire (`RLIMIT_AS`) et en temps, dans un espace réseau vide. Le document est audité
  d'abord (`imaging/svg_audit.py` : aucun DTD, script, `foreignObject`, police, référence hors du
  document, taille et nombre de nœuds bornés) et tout ce qui n'est pas clairement un SVG simple est
  refusé. Conséquence assumée : le texte d'une page SVG n'est pas dessiné (aucune police chargée).
  Repli : refuser les pages SVG comme en M1a (`content.untrusted_entry`).
- **webauthn** (py_webauthn, BSD-3-Clause) : passkeys (P8), plancher `>=3.0.1,<4` (3.0.1 publiée le
  2026-09-25) ; dépendances transitives verrouillées : cbor2 (MIT), cryptography (Apache-2.0 ou
  BSD-3-Clause), pyOpenSSL (Apache-2.0), pyasn1 et pyasn1-modules (BSD). Importée par le seul
  paquet `shelfhold.identity.webauthn` (contrat `webauthn-confined`) ; repli : `cryptography` et
  `cbor2` directement.
- **Authlib** (BSD-3-Clause, compatible AGPL-3.0) : connexion par OpenID Connect (EF-USR-004, M5
  L03, 06 Q73). **Exception écrite au gel de `uv.lock`** (figé par L00) : une seule dépendance
  ajoutée, plancher `>=1.6,<2`. Version 1.9.3, publiée le 2026-11-03, donc plus de 14 jours avant le
  2026-11-18 : aucune exception à `exclude-newer`. **Elle apporte `joserfc`** (BSD-3-Clause, même
  auteur, 1.8.0 publiée le 2026-11-03, déclarée elle aussi : `>=1.8,<2`), sa bibliothèque JOSE, que `authlib.jose` (dépréciée en
  1.9) remplace : la signature du jeton d'identité est vérifiée par `joserfc`, avec une liste fermée
  d'algorithmes asymétriques ; Authlib fournit le vérificateur PKCE et son défi S256. `cryptography`
  y est déjà. Aucun client HTTP d'Authlib n'est utilisé : le réseau est celui du Fetcher et de sa
  garde SSRF, jamais le leur (découverte, jeton et clés). Importées par le seul
  `shelfhold.identity.adapters.oidc_client` (contrat `authlib-confined`). Repli : `joserfc` et
  `cryptography` seuls (le flux est court : PKCE, un `POST` de jeton, une vérification de signature).
- **websockets** (BSD-3-Clause, compatible AGPL-3.0) : client WebSocket du hub de Kavita
  (`SeriesAdded`, `ScanSeries`), pour confirmer une arrivée plus tôt que le polling (EF-EXP-034, M4
  L37, D18). **Exception écrite au gel de `uv.lock`** (figé par L00) : une seule dépendance ajoutée,
  plancher `>=17.4,<18`. Version 17.4, publiée le 2026-10-30, donc plus de 14 jours avant le
  2026-11-17 : aucune exception à `exclude-newer`. **Python pur** : la roue `py3-none-any` est
  publiée et fonctionne partout, donc sur amd64 comme sur arm64 ; PyPI publie aussi des roues
  `cp313` `manylinux` pour `x86_64` et `aarch64` (une accélération facultative du masquage des
  trames, sans autre effet), toutes deux dans `uv.lock`. Aucune dépendance transitive. Importée par
  le seul `shelfhold.export.targets.kavita.events_transport` (contrat `websockets-confined`). Le
  jeton du hub vient de la clé dédiée (`POST /api/Plugin/authenticate`) ; la connexion part vers
  les adresses validées par la garde SSRF du fetcher, jamais résolues une seconde fois, TLS
  vérifié. Un événement n'est jamais une preuve et le polling reste actif : si le hub tombe, seul
  le transport est coupé. Repli : retirer la dépendance (le polling seul confirme, comme avant L37)
  ou `aiohttp`/`wsproto`. Signal de déclenchement : changement de licence, dépôt archivé, 12 mois
  sans release.
- **Contrats M1b (L00)** : `sablier-confined` (seul `integrations.sablier` appelle Sablier),
  `backup-dir-sole-writer` (seul `ops.backup` écrit le répertoire de sauvegarde, via
  `ops.backup.store`) et `third-party-plugins-single-loader` (seul `plugin_host` charge des
  plugins tiers : `importlib`, `pkgutil` et `runpy` sont interdits ailleurs, hors les lecteurs de
  ressources listés dans `ignore_imports`).
- **python-ulid** (MIT) : identifiants. **langcodes** (MIT) : `normalize_lang()` (ADR-0042) ; repli :
  table d'alias figée et versionnée. Contrat `lang-single-producer`.
- Développement : **hypothesis** (MPL-2.0), **syrupy** (MIT), **pytest-xdist** (MIT) et
  **xmlschema** (MIT) pour les suites `tests/property`, `tests/golden` et la validation de
  schémas ; **vulture** (MIT, M4) pour la passe de code mort sans appelant (`[tool.vulture]` dans
  `backend/pyproject.toml`, lue par l'opérateur : la garde bloquante reste
  `tools/reachability.py`).

### Exception à la fenêtre `exclude-newer`

Le verrou n'accepte que des publications vieilles de 14 jours (`exclude-newer`). Deux paquets font
l'objet d'une exception ciblée (`exclude-newer-package` dans `[tool.uv]`), parce que leur version
a été vérifiée explicitement :

- huey 3.5.4 (publiée le 2026-11-01), vérifiée par le spike S1 : plancher `>=3.5.4,<3.6`.
- apprise 2.1.0 (publiée le 2026-10-24), plancher d'ENF-SEC-016 : `>=2.1,<3`.

Retrait possible le 2026-11-15, quand les deux versions ont plus de 14 jours : supprimer la ligne
`exclude-newer-package` et relancer `uv lock`. Aucun autre paquet n'est concerné.

> **Note pour M1b L32** (M2 L00, 2026-11-10) : la date de levée (2026-11-15) n'est pas encore atteinte,
> la ligne est donc conservée. M1b L32 la retire à partir du 2026-11-15 (suppression de la ligne, puis
> `uv lock`) ; à défaut, le premier lot M2 qui touche à `backend/pyproject.toml` le fait.

- **tzdata** : base des fuseaux horaires (`Europe/Paris` pour la fenêtre de maintenance, ENF-OPS-014) ;
  l'image `python:slim` n'en contient pas et `zoneinfo` échouerait.
- **uvicorn**, **SQLAlchemy**, **Alembic**, **structlog**, **pydantic** : socle du squelette,
  versions minimales et verrou dans `backend/pyproject.toml` et `uv.lock` (racine de l'espace de travail uv).

## Harnais de test de l'atelier (M3 L02)

- **@axe-core/playwright** `4.14.0` (version exacte, `e2e/package.json` et son verrou) : contrôle
  d'accessibilité bloquant (`serious` et `critical`) joué par les projets `workshop-desktop` et
  `workshop-mobile`. Il tourne dans le navigateur de test, sur l'API locale : aucun accès réseau.
  `frontend/package.json` ne change pas : le script de contraste n'a pas de dépendance.

## Calcul du hash de la CSP (M3 L03)

- **parse5** `^8` (devDependency de `frontend/`, MIT, déjà présente via jsdom) : lit `app.html` pour
  en extraire le script de démarrage et calculer son hash au build. Un analyseur HTML remplace
  l'expression régulière sur les balises (`</script >`, casse, commentaires). Build seulement,
  rien n'est livré dans l'image.
