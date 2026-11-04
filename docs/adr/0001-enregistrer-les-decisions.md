---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 1)
decision-makers: mainteneur
---
# ADR-0001 — Enregistrer les décisions d'architecture

> **Amendée le 2026-11-02 (passe 1).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Shelfhold est maintenu par une seule personne, sur un dépôt public, avec un recours assumé aux assistants IA. Les choix structurants (langage, stockage, plugins, licence, posture légale) doivent rester lisibles et contestables dans la durée, par le mainteneur dans six mois comme par un contributeur. Sans trace écrite, les raisons d'un choix se perdent, et on rouvre le même débat.

## Facteurs de décision
- Coût d'écriture faible (une décision = un fichier court).
- Versionné avec le code et revu en PR.
- Format connu des contributeurs et des outils.
- Doit être lisible par les agents IA (AGENTS.md y renvoie).

## Options considérées
### A. MADR 4 dans `docs/adr/NNNN-titre.md`
- Avantages : format standard, gabarit léger, statut et options explicites, rendu par le site de docs Zensical.
- Inconvénients : discipline nécessaire pour mettre à jour les statuts (remplacé/déprécié).
### B. Wiki GitHub ou issues « décision »
- Avantages : zéro outillage.
- Inconvénients : hors du dépôt et des PR, non versionné avec le code, dispersé.
### C. Rien (README + commentaires)
- Avantages : aucun effort.
- Inconvénients : raisons perdues, débats rouverts, mauvais signal pour les contributeurs.

## Décision
Option A. Une ADR MADR 4 par décision structurante dans `docs/adr/`, numérotée sur 4 chiffres, rédigée en français (identifiants de code en anglais). Les décisions D0–D44 de `DECISIONS.md` servent de base ; chaque ADR cite les Dxx qu'elle formalise. Statuts : `proposé`, `accepté`, `déprécié`, `remplacé par ADR-NNNN`. Une ADR acceptée ne se réécrit pas, on la remplace.

## Conséquences
- Positives : historique auditable, revue facilitée, index généré dans la doc.
- Négatives : tâche de maintenance supplémentaire ; risque d'ADR obsolètes si le statut n'est pas tenu à jour.

## Risque principal
ADR qui divergent du code. Mitigation : case « ADR mise à jour ? » dans le gabarit de PR pour toute PR qui touche une frontière de module ou un contrat.

## Réversibilité
Haute : on peut changer de format sans toucher au code.

## Amendement passe 1 (2026-11-02)
- **Convention d'amendement** (passe 1) : une ADR acceptée peut recevoir une section datée « Amendement passe N » quand une revue modifie la décision sans la renverser ; le texte d'origine reste en place pour l'historique et l'amendement prévaut. Un renversement complet donne une nouvelle ADR (`remplacé par ADR-NNNN`) ou, exceptionnellement, une réécriture signalée en tête (cas d'ADR-0015, pour retirer des mentions que le projet ne doit plus publier).
- **Traçabilité de la revue** : les arbitrages A0–A38 de la passe 1 sont cités par leur numéro ; le journal des objections relie chaque objection à l'exigence (EF/ENF) ou à l'ADR qui l'intègre.
- **Nouvelles ADR** : ADR-0019 (nom), ADR-0020 (positionnement atelier), ADR-0021 (Series, Édition, identité des Unités), ADR-0022 (montage et correspondance de chemins), ADR-0023 (sauvegarde et export ouvert), ADR-0024 (contrat de données du SDK et compatibilité `api`), ADR-0025 (WakeHook), ADR-0026 (gouvernance et politique de versions).
- **Site de docs** : Zensical sur Pages arrive au palier M3 de la CI (A16) ; en M0–M2 les ADR sont lues dans le dépôt.
- Renommage du projet en Shelfhold (A0, ADR-0019).

## Exigences liées
ENF-DOC-001, ENF-QUAL-002, ENF-GOV-001.

## Sources
- https://pydevtools.com/handbook/reference/mkdocs-material/ (passage de Material for MkDocs à Zensical)
- https://github.com/home-assistant/core/blob/dev/AI_POLICY.md
