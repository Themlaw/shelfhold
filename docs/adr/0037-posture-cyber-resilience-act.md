---
status: accepté
date: 2026-11-03
decision-makers: mainteneur
---
# ADR-0037 — Posture face au Cyber Resilience Act

## Contexte et problème
Le Cyber Resilience Act (règlement UE 2024/2847) impose des obligations aux **fabricants** de produits comportant des éléments numériques mis sur le marché dans le cadre d'une activité commerciale, et un régime allégé aux **stewards** de logiciels libres (art. 24 : personne morale qui soutient durablement un logiciel libre destiné à des activités commerciales). Les obligations de signalement des vulnérabilités activement exploitées s'appliquent depuis le 11 septembre 2026 ; l'application complète est prévue le 11 décembre 2027. Les livrables ne mentionnaient pas le CRA (explorateur passe 4, R3), alors que la gouvernance envisage un hôte fiscal ou une association (B23) et qu'un intégrateur commercial (image de NAS, offre hébergée) deviendrait fabricant et demanderait SBOM, contact de sécurité, politique de divulgation et durée de support.

## Facteurs de décision
- Rester hors champ tant que le projet est bénévole et non monétisé.
- Repérer d'avance ce qui ferait basculer le projet.
- Donner aux intégrateurs ce qu'ils demanderont, sans prétendre à la conformité.
- Ne rien affirmer de juridique sans avis (Q27).

## Options considérées
### A. Ignorer le CRA (mainteneur individuel hors champ)
- Avantages : rien à écrire.
- Inconvénients : une structure ou un support payant pourrait faire basculer le projet sans que personne le remarque ; intégrateurs sans réponse.
### B. Posture écrite (`docs/legal/cra.md`) en M0, `SECURITY.md` complet avant 1.0, revue CRA obligatoire avant toute structure porteuse, question ajoutée à Q27
- Avantages : coût faible ; points de bascule visibles ; réponses prêtes pour les intégrateurs (SBOM et provenance déjà prévus en M1b).
- Inconvénients : document à tenir à jour ; ne remplace pas un avis juridique.
### C. Viser la conformité de fabricant dès la 1.0
- Avantages : prêt pour tout usage commercial.
- Inconvénients : charge disproportionnée pour un bénévole ; donnerait l'apparence d'une activité commerciale.

## Décision
Option B (arbitrage D3).
- **M0** : `docs/legal/cra.md`, factuel et non juridique : statut (mainteneur individuel, aucune monétisation, aucun don avant un hôte fiscal, fonctions payantes = Jamais), critères de bascule (structure porteuse, support payant, offre hébergée, collecte de données monétisée), revue à chaque revue de jalon.
- **Avant 1.0** : `SECURITY.md` complété (canal privé, accusé de réception, évaluation et correctif avec délais indicatifs, versions supportées `X.Y` courante et précédente, avis GHSA publiés), sans prétendre à la conformité ; SBOM et attestations en assets de release (ADR-0016).
- **Avant toute structure porteuse** : relecture de `docs/legal/cra.md`, question à l'avis juridique (Q27 point f), décision consignée dans un ADR (Q35).

## Conséquences
- Positives : statut clair ; un intégrateur trouve contact, délais et SBOM ; la gouvernance ne crée pas de structure sans y penser.
- Négatives : un document de plus à relire ; délais de divulgation à tenir au mieux.

## Risque principal
Interprétation erronée de la notion d'activité commerciale (dons, hôte fiscal) ; les sources divergent sur certains points *[non vérifié]*. Mitigation : avis juridique avant toute structure, aucune monétisation d'ici là.

## Réversibilité
Haute : posture documentaire ; passer à l'option C reste possible si une structure le justifie.

## Amendement passe 9 (2026-11-03)
U2.
- Les obligations de signalement de l'art. 14 s'appliquent depuis le 2026-09-11, aux fabricants ; la posture (projet non monétisé, hors champ en tant que fabricant) est inchangée ; `docs/legal/cra.md` est daté.

## Exigences liées
ENF-LEG-011, ENF-SEC-011, ENF-GOV-001, ENF-QUAL-007.

## Sources
- https://lwn.net/Articles/1040774
- https://writing.kemitchell.com/2024/01/02/CRA-Draft-Free-Open
- https://www.noze.it/en/insights/cra-commission-guidance/
- https://www.goodwinlaw.com/en/insights/publications/2026/09/alerts-lifesciences-technology-preparing-for-eu-cyber-resilience-act
- https://policy.openssf.org/CRA/stewards-one-pager.html
- https://freebsdfoundation.org/eu-cyber-resilience-act-cra/
- https://www.herodevs.com/blog-posts/cra-reporting-obligations-start-september-2026-what-eol-dependencies-mean-for-your-compliance
- `deliverables/review/explorateur-passe4.md` (R3)
- `deliverables/review/ARBITRAGES-passe4.md` (D3)
- `deliverables/review/chasseur-trous-passe9.md`, `deliverables/review/explorateur-passe9.md`, `deliverables/review/ARBITRAGES-passe9.md` (passe 9)
