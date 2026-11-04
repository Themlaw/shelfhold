---
status: accepté
date: 2026-11-02
amended: 2026-11-03 (passe 6)
decision-makers: mainteneur
---
# ADR-0032 — Domaine public apprécié par pays

## Contexte et problème
La posture légale (ADR-0015) présente Gutenberg et Standard Ebooks comme des sources légales natives. Ce sont des domaines publics **américains**. En France, la durée est la vie de l'auteur + 70 ans, **plus les prorogations de guerre**, **plus 30 ans** pour un auteur « mort pour la France » ; une **traduction** a ses propres droits (date de mort du traducteur). Exemple connu : *Le Petit Prince*, dans le domaine public presque partout depuis 2015, reste protégé en France jusqu'en 2032 ou 2033. Précédent : en 2018, Gutenberg a bloqué l'accès depuis l'Allemagne après une condamnation sur 18 titres, du domaine public aux États-Unis mais pas en Allemagne (P5). Les livrables ne parlaient ni de juridiction, ni de prorogation, ni de traducteur.

## Facteurs de décision
- Ne pas laisser croire qu'une œuvre est « légale » partout parce qu'elle l'est aux États-Unis.
- Ne pas bloquer l'utilisateur sur un calcul juridique incertain (le projet ne qualifie pas l'usage, ENF-LEG-006).
- Coût faible : quelques champs et un avertissement.
- Vaut pour chaque instance du projet AGPL, pas seulement pour le foyer du mainteneur.

## Options considérées
### A. Ne rien faire (vocabulaire « source légale »)
- Avantages : aucun travail.
- Inconvénients : trompeur hors des États-Unis ; risque pour les démos et les listes scolaires.
### B. Pays de l'instance + base des droits par source + avertissement indicatif non bloquant
- Avantages : informe au bon moment ; reste une aide, pas un verrou ; vocabulaire exact.
- Inconvénients : données de décès parfois absentes ; calcul approximatif (prorogations, « mort pour la France »).
### C. Blocage des œuvres possiblement protégées dans le pays de l'instance
- Avantages : prudence maximale.
- Inconvénients : faux positifs nombreux faute de données ; le projet se poserait en juge du droit applicable.

## Décision
Option B (arbitrage C18).
- Réglage **pays de l'instance** (`instance.jurisdiction`), déduit de la locale, affiché et confirmé dans l'assistant (Q32).
- Chaque source déclare `rights_basis` (`us_public_domain`, `worldwide_public_domain`, `licence`, `user_supplied`) et sa juridiction ; Gutenberg et Standard Ebooks = États-Unis.
- L'Édition porte `author_death_year`, `translator`, `translator_death_year` quand la source les fournit.
- Calcul **indicatif** (vie + 70 ans, prorogations françaises, « mort pour la France », traductions) → **avertissement non bloquant** (EF-SRC-024).
- Vocabulaire : « libre de droits **aux États-Unis** » plutôt que « source légale » (ENF-LEG-008).
- Question ajoutée au juriste (Q27).

## Conséquences
- Positives : documentation et UI exactes ; démos et listes scolaires choisies en connaissance de cause.
- Négatives : données à maintenir dans les adaptateurs ; avertissements parfois absents faute de dates.

## Risque principal
Un avertissement absent (date de décès inconnue) pris pour une garantie. Mitigation : libellé « aucune information sur les droits dans votre pays », jamais « libre de droits en France » ; le calcul n'est pas un avis juridique.

## Réversibilité
Haute : champs additionnels et avertissement retirables sans migration de contenu.

## Amendement passe 6 (2026-11-03)
F11 (U3, U4).
- Les **dates de vie** sont portées par la personne (`contributor.birth_date?`, `death_date?`, partielles avec précision ; EF-BIB-046, EF-BIB-047), plus par l'Édition ; les champs `author_death_year?`, `translator_death_year?` de l'Édition deviennent un cache dérivé.
- Le calcul indicatif porte sur **toutes** les contributions à droits propres (auteur, illustrateur, traducteur, adaptateur, préfacier) via `contribution.role` (codes MARC relator) : la date la plus tardive l'emporte. Test : un livre illustré dont l'illustrateur est mort il y a 50 ans affiche l'avertissement pour la France.
- Pour le droit américain, l'année qui compte est celle de **première publication** de l'Œuvre (`work.first_published`), pas celle de l'Édition.

## Exigences liées
EF-SRC-007, EF-SRC-011, EF-SRC-024, EF-BIB-033, ENF-LEG-006, ENF-LEG-008, EF-BIB-046, EF-BIB-047.

## Sources
- https://fr.wikisource.org/wiki/Auteur:Antoine_de_Saint-Exup%C3%A9ry
- https://www.slate.fr/story/97573/saint-exupery-domaine-public
- https://teleread.org/2018/03/03/project-gutenberg-blocks-german-users-after-court-rules-in-favor-of-holtzbrinck-subsidiary/
- https://archive.techdirt.com/articles/20180306/03423339363/project-gutenberg-blocks-access-germany-to-all-public-domain-books-because-of-local-copyright-claim-18-them.shtml
- `deliverables/review/explorateur-passe3.md` (P5), `deliverables/review/ARBITRAGES-passe3.md` (C18)
