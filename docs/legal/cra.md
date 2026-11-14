# Posture Cyber Resilience Act (CRA)

Source : [ADR-0037](../adr/0037-posture-cyber-resilience-act.md), ENF-LEG-011. Document factuel, **ce n'est pas un avis juridique** et il n'affirme aucune conformité.

## Statut actuel

- Mainteneur individuel (compte personnel `Themlaw`), projet bénévole.
- Aucune monétisation. Aucun don avant l'existence d'un hôte fiscal. Fonctions payantes exclues.
- Pas d'offre hébergée, pas de collecte de données (aucune télémétrie).

Le règlement (UE) 2024/2847 vise les fabricants de produits mis sur le marché dans le cadre d'une activité commerciale, avec un régime allégé pour les « stewards » de logiciels libres. Les obligations de signalement des vulnérabilités activement exploitées s'appliquent aux fabricants à partir du 11 septembre 2026 ; l'application complète est prévue le 11 décembre 2027 *[dates à revérifier]*.

## Critères de bascule (revue obligatoire)

Toute évolution ci-dessous impose de relire ce document, de poser la question à un juriste et de consigner la décision dans une ADR :

- création d'une structure porteuse (association, hôte fiscal) ;
- support payant ou monétisation d'une forme quelconque ;
- offre hébergée ;
- collecte de données monétisée.

## Ce que le projet fournit aux intégrateurs

Le projet ne prétend à aucune conformité : ce sont des faits vérifiables, que l'intégrateur apprécie lui-même.

- **Canal de signalement privé et avis GHSA** publiés après correctif ([SECURITY.md](https://github.com/Themlaw/shelfhold/blob/main/SECURITY.md)).
- **Délais** (au mieux, sans garantie) : accusé de réception ≤ 7 jours, correctif visé ≤ 90 jours.
- **Versions supportées** : la version mineure `X.Y` courante et la précédente ; avant 1.0, la dernière seulement.
- **SBOM SPDX et attestations de provenance** en assets de chaque release (palier M1b, [ADR-0016](../adr/0016-ci-cd-supply-chain.md)).
- **Images signées avec cosign** (sans clé, identité du workflow de publication), sur GHCR et, si le mainteneur l'a configuré, sur un second registre ; la vérification est décrite dans [vérifier les images](../admin/verifier-les-images.md) (palier M3).
- **OpenSSF Scorecard** hebdomadaire sur le dépôt, résultat dans l'onglet *Security* (palier M3).
- Actions de CI épinglées par SHA, DCO obligatoire, aucune télémétrie.

## Revue

À chaque revue de jalon.

| Date | Résultat |
|---|---|
| | à compléter |
