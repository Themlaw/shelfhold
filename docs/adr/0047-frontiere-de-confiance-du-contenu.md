---
status: accepté
date: 2026-11-03
amended: 2026-11-03 (passe 10) ; 2026-11-03 (passe 11) ; 2026-11-12 (M2 L33)
decision-makers: mainteneur
---
# ADR-0047 — Frontière de confiance du contenu

## Contexte et problème
Shelfhold définit déjà une frontière de confiance pour ce qu'il **sert** (origine dédiée, CSP `sandbox`, ENF-SEC-004) et pour les **métadonnées** qu'il exporte (texte brut ou assaini par nh3, ENF-SEC-015). Il n'en définissait pas pour le **corps** des fichiers qu'il range dans la destination : un EPUB reçu en *passthrough* (lncrawl, OPDS de tiers, dossier d'import) part tel quel vers Kavita.

L'explorateur de la passe 9 (X1) a vérifié dans le code de Kavita (branche `develop`, 2026-11-03) que le lecteur de livres injecte la page XHTML avec `bypassSecurityTrustHtml`, que le jeton de session **et** le jeton de rafraîchissement vivent dans `localStorage`, et qu'aucune `Content-Security-Policy` n'est posée. Un précédent public existe (CVE-2024-39307, XSS par EPUB piégé, vol du jeton puis lecture des réglages). Le contenu de lncrawl est du HTML récupéré sur des sites tiers par des crawlers que Shelfhold ne contrôle pas. Shelfhold deviendrait ainsi un relais d'injection vers tous les comptes du foyer, enfants et admin compris.

La question d'architecture est **où** placer l'assainissement par rapport aux empreintes (ADR-0043 : `content_hash` de l'original, `file_hash` du rendu) et à ce que garde `/store`.

## Facteurs de décision
- Aucune exposition livrée : la règle doit précéder le premier adaptateur qui exporte un EPUB récupéré sur le web (M2).
- Ne pas casser l'identité : `content_hash`, comptage des exemplaires et restauration (ADR-0043, ADR-0044) restent cohérents.
- Fichiers achetés et tatoués intouchés hors de l'OPF (ADR-0033).
- Défense qui ne dépend pas de la destination (Kavita, et demain d'autres lecteurs).

## Options considérées
### A. Ne rien faire : la destination se défend
- Avantages : aucun code ; octets reçus intacts partout.
- Inconvénients : la destination ne se défend pas aujourd'hui ; Shelfhold relaie l'injection.
### B. Rendu assaini par destination (au moment de l'export)
- Avantages : l'original reste l'unique objet stocké ; règles adaptables par destination.
- Inconvénients : en mode passage, la copie de la destination est la seule qui survit quand la source disparaît ; la « récupération de l'original » depuis le fichier rendu devient fausse ; règles différentes selon la destination, donc plusieurs fichiers à tester.
### C. Dérivé canonique neutralisé, unique, produit à l'ingestion (retenue)
- Avantages : un seul fichier assaini, déterministe, quelle que soit la destination ; identité claire (`content_hash` = original, `derived_hash` = dérivé) ; récupération honnête (`extracted_derived`).
- Inconvénients : réécriture du XHTML des EPUB `untrusted` (rendu parfois appauvri) ; une version de neutraliseur de plus à maintenir.

## Décision
Option C (arbitrage J1).
- Chaque source déclare **`content_trust: publisher | untrusted`** au manifeste, **défaut `untrusted`** ; `publisher` : Gutenberg par ses catalogues officiels, Standard Ebooks, achats sans DRM importés comme tels ; `untrusted` : services connectés qui récupèrent du web, flux OPDS de tiers, import non qualifié. Champ du contrat SDK **dès M1a**.
- EPUB `untrusted` : **`xhtml-neutralizer@vN`** produit, **avant tout export**, un dérivé canonique déterministe : retrait des scripts, gestionnaires d'événements, URL `javascript:` et `data:text/html`, iframes, objets, `embed`, formulaires, ressources distantes ; SVG neutralisé (`<script>`, `foreignObject`) ; liste blanche nh3 étendue au XHTML d'EPUB.
- `content_hash` = original ; `derived_hash` + `derived_by` enregistrés ; `file_hash` sur le rendu du dérivé. L'original reste dans le staging puis dans `/store` quand il existe. Une récupération depuis une destination est **`extracted_derived`**, jamais `extracted`.
- CBZ et supports d'images : entrées SVG et formats hors liste blanche **refusés** (M1a) ou **rastérisés** (M2) ; extension = octets magiques.
- Fichiers `publisher` et `watermarked` : jamais réécrits hors de l'OPF (ADR-0033).
- Kit de conformité : corpus d'EPUB piégés (XHTML, SVG).
- Invariant I35.

## Conséquences
- Positives : plus de relais d'injection par le contenu exporté ; identité et restauration cohérentes ; même fichier vers toutes les destinations.
- Négatives : certains EPUB web perdent une mise en forme dynamique ; un dérivé à recalculer quand le neutraliseur change de version (job de maintenance, nouveau `file_hash`, chemin figé inchangé) ; un EPUB `untrusted` en mode passage n'existe plus en original une fois la source disparue.

## Risque principal
Contournement du neutraliseur par une construction non prévue (entités, espaces de noms XML, CSS `url()`). Mitigation : liste blanche (et non liste noire), analyse par `safe_parse_xml()` (ENF-SEC-014), corpus piégé enrichi à chaque avis connu, tests de propriétés, version du neutraliseur dans chaque empreinte pour pouvoir régénérer.

## Réversibilité
Moyenne : le dérivé est recalculable depuis l'original quand il est conservé ; un fichier déjà rangé se remplace par réexport. Abandonner la neutralisation rouvrirait l'exposition.

## Amendement passe 10 (2026-11-03)
K9 (GAP-10-06, GAP-10-07, GAP-10-08, GAP-10-14).
- **Tatouage heuristique** : un `watermarked` posé par l'heuristique est une suggestion ; il ne dispense jamais du neutraliseur un EPUB `untrusted`. Seule l'attestation d'achat (`publisher`) en dispense. Le neutraliseur ne touche que XHTML et SVG, jamais le tatouage de l'OPF ; un fichier `untrusted` + `watermarked` sans attestation est retenu « à qualifier ». Invariant I45.
- **Original gardé en mode passage** : l'original d'un EPUB `untrusted` est conservé dans `/store` même en mode passage (ADR-0028). La grille disait « dérivé recalculable depuis l'original conservé » ; c'était faux en mode passage avant cet amendement.
- **Mise à jour de sécurité du neutraliseur** : migration explicite (ENF-OPS-021), depuis l'original s'il existe, sinon depuis le dérivé rangé, avec `derived_chain` ; `derived_hash` = `content_hash()@v` du dérivé hors OPF, avec `derived_hash_by` ; un CBZ rastérisé (M2) est un dérivé déclaré.
- **Plugins tiers** : `content_trust: publisher` déclaré par un plugin tiers n'est retenu que sur attestation de l'admin.
- **Contenu adopté** : les EPUB `untrusted` déjà dans Kavita avant l'adoption gardent leurs scripts (Shelfhold n'en est pas le relais) ; le rapport d'adoption (M2) les signale.

## Amendement passe 11 (2026-11-03)
GAP-11-09, GAP-11-13.
- L'attestation de `publisher` et de `content_trust` s'applique à **tout plugin non first-party dès M2** (premier jalon où un EPUB récupéré sur le web est exporté), isolé ou non, et pas seulement en M6.
- Une destination `pull` (OPDS, kosync en M5, Kobo en M6) reçoit toujours un **rendu du dérivé neutralisé** ; l'original `untrusted` gardé dans `/store` n'est jamais servi (I35, EF-BIB-045).

## Exigences liées
EF-BIB-051, ENF-SEC-015, ENF-SEC-004, ENF-SEC-003, EF-SRC-001, EF-SRC-010, EF-SRC-006, EF-BIB-006, EF-BIB-048, EF-BIB-041, EF-BIB-045, EF-EXP-065.

## Sources
- `deliverables/review/explorateur-passe9.md` (X1), `deliverables/review/ARBITRAGES-passe9.md` (J1)
- https://github.com/Kareadita/Kavita/blob/develop/UI/Web/src/app/book-reader/_components/book-reader/book-reader.component.ts (`bypassSecurityTrustHtml`)
- https://github.com/Kareadita/Kavita/blob/develop/UI/Web/src/app/_models/user/user.ts (jetons dans `localStorage`)
- https://github.com/Kareadita/Kavita/security/advisories/GHSA-r4qc-3w52-2v84 (CVE-2024-39307)
- https://github.com/messense/nh3 · https://www.w3.org/TR/epub-33/ · https://github.com/johnfactotum/foliate-js#security
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)

## Amendement M2 (2026-11-12) : PDF (D9) et SVG (D10)

Lot M2 L33. **PDF (D9).** Le neutraliseur ne traite que l'EPUB, et un PDF peut porter du JavaScript que le lecteur de Kavita exécuterait. Un PDF `untrusted` est donc rangé tel quel et **non exporté** (`content.untrusted_pdf`, visible « À ranger » avec sa raison) ; un PDF `publisher` (achat déclaré) est exporté tel quel. Un PDF n'est de toute façon rangé qu'après une lecture stricte par **pypdf dans le worker de décodage** : sous-processus borné en mémoire et en temps, sans chiffrement, sans fichier joint ni enveloppe d'un fichier protégé ; au moindre doute il est refusé et reste en place (PR #154, M2 L20, `docs/dependencies.md`). **SVG (D10).** Une entrée SVG d'un CBZ `untrusted` est **rastérisée** en un dérivé déclaré, par un moteur de rendu sans script ni chargement externe (resvg-py) appelé dans le worker de décodage (sans réseau, `RLIMIT_AS`), après un audit qui refuse tout ce qui n'est pas clairement un SVG simple ; sans cela le refus de M1a s'applique (PR #164, M2 L10).
