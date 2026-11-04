---
status: accepté
date: 2026-11-03
decision-makers: mainteneur
---
# ADR-0036 — DCO et politique de contribution ; exception de plugin dès le premier commit public

## Contexte et problème
L'application est sous AGPL-3.0-or-later avec un SDK MIT (ADR-0014). Une exception de plugin (permission additionnelle au sens de l'AGPL §7) était prévue, mais seulement après une relecture juridique (Q17), avant M6. Or une permission additionnelle ne peut être accordée que par les titulaires des droits. Dès la première contribution externe fusionnée, ajouter l'exception exigerait l'accord de chaque contributeur, ou la réécriture de son code ; la même difficulté bloquerait toute relicence future. Aucun livrable ne prévoyait de DCO obligatoire, de CLA ni de clause entrante (explorateur passe 4, R2). L'analyse de regret (R5) en fait la décision la plus irréversible du projet.

## Facteurs de décision
- Garder possible l'exception de plugin quels que soient les contributeurs.
- Ne pas dissuader les contributions (pas de cession de droits).
- Traçabilité de l'origine de chaque commit.
- Pouvoir corriger le texte de l'exception après relecture.

## Options considérées
### A. Exception ajoutée après relecture (plan de la passe 2), DCO conseillé
- Avantages : texte relu avant publication.
- Inconvénients : impossible à ajouter sans l'accord de tous dès qu'un contributeur externe a fusionné du code.
### B. Premier commit public : `LICENSE` AGPL + exception provisoire ; DCO 1.1 obligatoire, vérifié en CI ; clause entrante dans `CONTRIBUTING.md` ; pas de CLA
- Avantages : toutes les contributions sont reçues avec l'exception ; une permission additionnelle peut ensuite être retirée ou resserrée (AGPL §7) ; DCO léger et répandu.
- Inconvénients : texte provisoire publié avant relecture ; une ligne `Signed-off-by:` à ajouter à chaque commit.
### C. CLA de cession ou de licence large
- Avantages : liberté totale de relicence.
- Inconvénients : lourd, dissuasif, demande une structure pour détenir les droits.

## Décision
Option B (arbitrage D2).
- **Premier commit public** : `LICENSE` AGPL-3.0-or-later avec l'exception de plugin (modèle FSF « linking over a controlled interface », limitée au code dont le projet est titulaire, API publique du SDK seulement) sous un en-tête « provisoire : texte relu avant 1.0 et avant M6 (Q17) » ; `sdk/` sous MIT.
- **DCO 1.1 obligatoire** : `git commit -s` ; workflow `dco.yml` qui bloque toute PR dont un commit n'a pas de `Signed-off-by:`, y compris ceux du mainteneur ; test `test_commits_signed_off`.
- **Clause entrante** (`CONTRIBUTING.md`) : toute contribution est reçue sous AGPL-3.0-or-later avec l'exception de plugin de `LICENSE` (et sous MIT pour `sdk/`), y compris dans la version relue de cette exception.
- Pas de CLA.
- Q17 passe de « ouvert, publication avant M6 » à « tranché sur le principe, relecture avant 1.0 et M6 ».

## Conséquences
- Positives : l'exception reste possible et ajustable ; l'origine de chaque commit est tracée ; aucune cession de droits.
- Négatives : un texte provisoire circule avant relecture ; contrôle CI de plus ; les contributeurs doivent signer leurs commits.

## Risque principal
Texte provisoire trop large, qui autoriserait des plugins propriétaires dépendant d'API internes. Mitigation : périmètre limité à l'API publique du SDK ; la relecture peut resserrer le texte pour les versions suivantes avant tout plugin tiers (M6).

## Réversibilité
Moyenne : l'exception peut être resserrée ou retirée dans les versions suivantes ; les versions déjà distribuées la gardent. Le DCO peut être abandonné à tout moment, mais les commits déjà signés restent tracés.

## Exigences liées
ENF-LEG-010, ENF-GOV-001, EF-SRC-030.

## Sources
- https://www.gnu.org/licenses/agpl-3.0.html
- https://www.gnu.org/licenses/gpl-faq.html#GPLIncompatibleLibs
- https://developercertificate.org/
- `deliverables/review/explorateur-passe4.md` (R2, R5)
- `deliverables/review/ARBITRAGES-passe4.md` (D2)
