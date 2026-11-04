---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 1) ; 2026-11-02 (passe 2) ; 2026-11-03 (passe 6) ; 2026-11-03 (passe 11)
decision-makers: mainteneur
---
# ADR-0014 — Licence : AGPL-3.0-or-later pour l'app, MIT pour le SDK, exception plugins

> **Amendée le 2026-11-02 (passes 1 et 2).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Projet public auto-hébergé. On veut empêcher un fork hébergé fermé, rester compatible avec les briques réutilisées (foliate-js MIT, pdf.js Apache-2.0, ebooklib AGPL, code GPL de Kavita si besoin) et laisser les auteurs de plugins libres de leur licence.

## Facteurs de décision
- Protection contre un SaaS fermé (clause réseau).
- Compatibilité des dépendances.
- Liberté des auteurs de plugins.
- Confiance de la communauté auto-hébergée.

## Options considérées
### A. AGPL-3.0-or-later (app) + MIT (paquet `shelfhold-sdk`) + exception plugin explicite
- Avantages : clause réseau ; compatible ebooklib (AGPL), PyMuPDF, code GPL-3 de Kavita ; c'est la licence des pairs (Immich, Mealie, BookOrbit, Grimmory) ; plugins sous licence libre de leur choix grâce au SDK MIT et à l'exception.
- Inconvénients : un plugin en processus est discutablement une œuvre dérivée (d'où l'exception) ; certaines entreprises évitent l'AGPL.
### B. MIT/Apache-2.0 partout
- Avantages : adoption maximale.
- Inconvénients : interdit ebooklib, PyMuPDF et le code GPL ; permet un fork SaaS fermé.
### C. GPL-3.0
- Avantages : copyleft connu.
- Inconvénients : pas de clause réseau.

## Décision
Option A (D25). L'exception plugin est rédigée dans le style des linking exceptions GPL et figure dans `LICENSE` et la doc. Suwayomi (MPL-2.0), lncrawl (GPL-3.0) et pandoc (GPL-2.0) tournent **hors processus**. Si lncrawl est importé comme paquet, la GPL-3.0 reste compatible avec l'AGPL-3.0 (la combinaison est permise par la section 13 de la GPLv3 ; à confirmer par une relecture juridique). EmbedPDF : uniquement les paquets Apache. Les dépendances GPL-2.0-only sont interdites (Apache-2.0 incompatible). `THIRD_PARTY_NOTICES` est généré en CI (pip-licenses / license-checker). Le fork de BookOrbit est exclu notamment à cause de son `ADDITIONAL_TERMS.md` (mention « Powered by BookOrbit » obligatoire).

## Conséquences
- Positives : alignement avec l'écosystème ; réutilisation de bibliothèques AGPL et GPL.
- Négatives : vérification de licences en CI ; contributeurs invités à accepter l'AGPL (DCO recommandé).

## Risque principal
Contamination par une dépendance incompatible (GPL-2.0-only, licence propriétaire). Mitigation : liste d'autorisation des licences vérifiée en CI.

## Réversibilité
Faible : relicencier exige l'accord de tous les contributeurs (sauf CLA, non prévu).

## Amendement passe 1 (2026-11-02)
- **Portée de l'exception plugin** (A19, LEG-07) : l'exception ne couvre que le **code dont le projet détient les droits** ; elle ne peut pas relicencier du code tiers (GPL de lncrawl, etc.). Rédaction en M0, **relecture par un juriste bloquante avant M6** (publication du SDK et des plugins tiers) et avant la 1.0 publique (Q17, Q27).
- **SDK** : paquet `shelfhold-sdk` (MIT), interne au monorepo en V1, publié en M6 (ADR-0024).
- **lncrawl** n'est **jamais importé** comme paquet : adaptateur HTTP vers l'instance de l'utilisateur (ADR-0008) ; la question de compatibilité GPL/AGPL ne se pose donc pas en V1.
- **Calibre** : on reprend la forme de l'API `Source` des fournisseurs de métadonnées, pas le code (GPL-3.0) (closure-A).
- **Marques** : mention « marques de leurs titulaires, sans affiliation » (ENF-LEG-004, LEG-10).
- **Contributions** : DCO, e-mail de commit `noreply`, dépôt sous organisation (ENF-GOV-001).

## Amendement passe 2 (2026-11-02)
- **L'exception plugin n'est pas publiée avant la relecture juridique Q17** (B24) : tant qu'elle n'est pas faite, le dépôt reste **AGPL-3.0-or-later + SDK MIT**, ce qui est sûr (les plugins hors processus restent libres de licence). Raison : ajouter une permission est facile, la retirer est difficile une fois publiée (les versions distribuées la gardent).
- **Formulation de référence** (`research/10-closure-C.md` §5) : modèle FSF « linking over a controlled interface » (permission additionnelle §7) et précédent CondationCMS — modules communiquant **uniquement** par l'API plugin publique et documentée (paquet SDK MIT), installables et retirables sans modifier le cœur ; API internes exclues ; l'exception tombe si l'interface est modifiée ; elle ne couvre que le code détenu par le projet. Repli sans exception : modèle Grafana (cœur AGPL, SDK permissif).

## Amendement passe 4 (2026-11-03)
Exception de plugin (D2, R2) : l'exception figure **dès le premier commit public**, sous forme provisoire, avec le DCO et une clause entrante (ADR-0036). Le raisonnement de la passe 2 (« ajouter une permission est facile ») ne tient que tant que le projet est seul titulaire : après la première contribution externe, l'ajout exigerait l'accord de chaque contributeur, alors que le retrait d'une permission additionnelle reste toujours possible (AGPL §7). La relecture Q17 peut donc resserrer le texte avant 1.0 et avant M6.

## Amendement passe 6 (2026-11-03)
F12 (GAP-6-20).
- **AGPL §13** : les membres du foyer interagissent avec Shelfhold par le réseau. Un lien « Code source » est offert sur la page de connexion neutre et `source_url` figure dans `/api/v1/about` dès M1a, puis dans le pied de page de l'UI en M3 (ENF-LEG-012). L'URL est configurable (défaut : dépôt et tag de la version) pour qu'un admin qui modifie le code puisse pointer vers sa version.

## Amendement passe 11 (2026-11-03)
GAP-11-15, M0. Frontière outillée entre le SDK MIT et le cœur AGPL : `sdk/` est un paquet séparé avec sa propre `LICENSE` MIT et des en-têtes SPDX par fichier ; un contrat import-linter interdit au SDK d'importer le cœur ; `CONTRIBUTING.md` : aucun code ne passe du cœur au SDK sans l'accord de ses auteurs (une contribution AGPL versée au paquet MIT exigerait leur accord).

## Exigences liées
ENF-DOC-001, ENF-SEC-007, ENF-LEG-001, ENF-LEG-004, ENF-GOV-001, ENF-LEG-010, ENF-LEG-012, EF-SRC-001.

## Sources
- https://github.com/johnfactotum/foliate-js
- https://github.com/mozilla/pdf.js
- https://github.com/aerkalov/ebooklib
- https://github.com/pymupdf/PyMuPDF
- https://github.com/embedpdf/embed-pdf-viewer
- https://github.com/bookorbit/bookorbit
- https://github.com/immich-app/immich
- https://github.com/mealie-recipes/mealie
- https://github.com/kovidgoyal/calibre/blob/master/src/calibre/ebooks/metadata/sources/base.py
- `deliverables/review/explorateur-passe4.md` (R2)
- https://www.gnu.org/licenses/agpl-3.0.html
- https://developercertificate.org/
- `deliverables/review/ARBITRAGES-passe4.md`
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)
