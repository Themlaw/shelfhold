---
status: accepté (livraison M5, rôle secondaire)
date: 2026-11-02
amended: 2026-11-02 (passe 1) ; 2026-11-03 (passe 7) ; 2026-11-03 (passe 12)
decision-makers: mainteneur
---
# ADR-0012 — Lecteur intégré : foliate-js, pdf.js, lecteur manga/webtoon maison

> **Amendée le 2026-11-02 (passe 1).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Le lecteur intégré vient après le noyau d'export (D0b), mais il doit couvrir EPUB (reflow et fixed), PDF, manga paginé RTL et webtoon vertical, avec un contenu non fiable (EPUB scrapés) et des locators Readium (D30).

## Facteurs de décision
- Couverture des formats ; maintenance ; licence compatible AGPL.
- Sécurité (scripts EPUB) ; locators CFI.
- Performance sur téléphones modestes et navigateurs e-ink.

## Options considérées
### EPUB
- **foliate-js** (MIT) — Avantages : reflow + fixed, MOBI/AZW3, FB2, CBZ, CFI, TTS, parseur OPDS ; utilisé par Readest, Grimmory, BookOrbit. Inconvénients : « not stable », pas de release (sous-module git recommandé) ; impose une CSP stricte.
- **Readium ts-toolkit** (BSD-3) — Avantages : le plus conforme aux standards. Inconvénients : exige un streamer (go-toolkit/cli) ; plus d'intégration.
- **epub.js** (BSD-2) — Avantages : connu. Inconvénients : dernière release v0.3.88 (2020), fixed-layout faible ; à éviter.
### PDF
- **pdf.js** (Apache-2.0) — Avantages : standard de fait. Inconvénients : lourd.
- **EmbedPDF** (PDFium WASM) — Avantages : annotations, recherche. Inconvénients : seuls les paquets Apache sont utilisables (serveur sous FCL).
### Manga/webtoon
- **Maison** (références : Komga MIT, Suwayomi-WebUI MPL) — Avantages : petit périmètre (liste d'images, IntersectionObserver, préchargement, RTL, doubles pages). Inconvénients : à écrire et tester.
- **Composant npm existant** — Inconvénients : aucun maintenu ; Villain (abandonné) et Okuma-Reader (inactif) ne servent que de référence.

## Décision
D21, D22 : foliate-js épinglé par commit pour EPUB, MOBI et FB2 ; pdf.js pour le PDF (EmbedPDF Apache en alternative) ; lecteur manga/webtoon maison. Le contenu livre est servi dans une iframe `sandbox` sans `allow-scripts`, sous CSP stricte (`default-src 'none'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'`), avec DOMPurify côté client en plus de nh3 côté serveur. Réglages en trois couches : appareil > série > mode. Readium CSS est utilisable pour la typographie. Le code de Kavita (GPL) est réutilisable grâce à l'AGPL, mais n'est pas prévu.

## Conséquences
- Positives : formats couverts sans moteur maison ; licences compatibles ; isolation forte.
- Négatives : dépendance à un projet sans release (le fork Readest est l'alternative maintenue) ; deux moteurs (foliate et pdf.js) en plus du lecteur d'images.

## Risque principal
Instabilité de foliate-js (R13). Mitigation : épinglage par commit, tests Playwright du lecteur sur le corpus golden, mises à jour via Renovate avec revue.

## Réversibilité
Moyenne : le lecteur est derrière une interface `ReaderEngine` côté front ; passer à Readium demanderait un streamer côté backend.

## Amendement passe 1 (2026-11-02)
- **Rôle secondaire, livraison en M5** (A1, A3, ADR-0020) : Kavita/Kover est le lieu de lecture principal de la famille. Le lecteur intégré sert à l'aperçu et à la lecture des œuvres non exportées ; il n'est pas dans la V1.
- **foliate-js vendu en sous-module git** épinglé par commit, derrière l'interface `ReaderEngine` : le paquet npm `foliate-js` est publié par un tiers, on n'en dépend pas (09-closure-B). Faits relevés : `tts.js` produit du SSML ; `overlayer`, `epubcfi` et `search.js` sont lents sur gros livres ; `dict.js` lit StarDict ; Web Speech limité à 32 767 caractères par énoncé ; Readium ts-toolkit (Decorations) reste l'alternative.
- **Isolation** (A26, ENF-SEC-004) : ressources de livre servies depuis une **origine/chemin dédié** avec `Content-Security-Policy: sandbox` par réponse, `Content-Disposition: attachment` pour SVG/HTML ; CSP du lecteur testée (`font-src`, `media-src`, `frame-src blob:`).
- **E-ink** (A14) : page **`/lite`** HTML rendue côté serveur, sans JS, pour Kobo (WebKit 538.1 sans ES6) et Kindle ≤ 5.12.4 ; Kindle ≥ 5.16.4 embarque Chromium (version non documentée) ; le thème e-ink de la SPA est « au mieux » (EF-LEC-010, ENF-COMP-002). KOReader + kosync reste la voie principale pour les liseuses.
- **Réglages** : priorité **par type** (sens/mode : série > mode ; confort : appareil > défaut) (EF-LEC-006).
- PDF reste « Plus tard » (EF-LEC-004).

## Amendement passe 7 (2026-11-03)
G9, V1, V2.
- La position de lecture est **ancrée sur le fichier lu** : `reading_position(user_id, export_item_id, content_hash, locator, client_ts, server_ts)` ; la progression par Unité (`reading_progress`) est une vue dérivée par `unit_coverage` (ADR-0045, EF-LEC-014). Le lecteur, kosync et la sync Kavita écrivent l'ancre ; le suivi et « Continuer la lecture » lisent la projection.
- `document_alias` pointe vers `export_item_id` : un hash de tome résout un seul support (EF-LEC-009).
- Fusion hors ligne (PWA, KOReader) : `client_ts` le plus récent, borné par `server_ts`, historique conservé, conflit proposé à l'utilisateur (EF-LEC-007).
- Une position prise sur un autre contenu (upgrade d'équipe, `rolling_tail`, réécriture) est transposée et marquée `approximate` (invariant I30).

## Amendement passe 9 (2026-11-03)
U4.
- pdf.js est épinglé et instancié avec `isEvalSupported: false` (CVE-2024-4367) ; le lecteur EPUB lit le dérivé neutralisé d'un EPUB `untrusted` (ADR-0047), en plus de l'isolation par origine et CSP `sandbox`.

## Exigences liées
EF-LEC-001, EF-LEC-002, EF-LEC-003, EF-LEC-004, EF-LEC-005, EF-LEC-006, EF-LEC-010, EF-LEC-012, ENF-SEC-004, ENF-PERF-006, ENF-A11Y-002, ENF-A11Y-003, ENF-COMP-002, EF-LEC-014, EF-LEC-007, EF-LEC-009.

## Sources
- https://github.com/johnfactotum/foliate-js
- https://github.com/readest/foliate-js
- https://github.com/readium/ts-toolkit
- https://github.com/readium/css
- https://github.com/futurepress/epub.js
- https://github.com/mozilla/pdf.js
- https://github.com/embedpdf/embed-pdf-viewer
- https://github.com/Suwayomi/Suwayomi-WebUI
- https://github.com/gotson/komga
- https://github.com/DrMint/Okuma-Reader
- https://github.com/readium/ts-toolkit/blob/main/navigator/docs/epub/Decorations.md
- https://developer.mozilla.org/en-US/docs/Web/API/SpeechSynthesisUtterance/text
- https://github.com/mikespub-org/seblucas-cops/issues/53
- https://broken-links.com/2012/07/27/browser-review-kobo-touch/
- `deliverables/review/chasseur-trous-passe7.md`, `deliverables/review/explorateur-passe7.md`, `deliverables/review/ARBITRAGES-passe7.md` (passe 7)
- `deliverables/review/chasseur-trous-passe9.md`, `deliverables/review/explorateur-passe9.md`, `deliverables/review/ARBITRAGES-passe9.md` (passe 9)
- https://github.com/advisories/GHSA-wgrm-67xf-hhpq (pdf.js, CVE-2024-4367)

## Amendement passe 12 (2026-11-03)
M10 (AA4, AA5).

- **Copies hors ligne (PWA, M5)** : à la reconnexion, le service worker relit `?since=` (ADR-0006) et **évince** toute Œuvre supprimée, purgée ou devenue invisible pour le profil ; test `test_offline_copy_evicted_after_reclassification`. Les fichiers déjà téléchargés par OPDS ou `/lite` ne peuvent pas être repris : risque résiduel accepté R66 (`06`).
- **Ancre** : une Œuvre non exportée lue depuis sa copie conservée a une position ancrée sur `content` (ADR-0045) ; le lecteur lit le dérivé neutralisé, jamais l'original `untrusted`.

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)
