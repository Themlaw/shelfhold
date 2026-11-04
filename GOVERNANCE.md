# Gouvernance

## Mainteneur

Le projet est maintenu par un mainteneur unique, le compte GitHub **Themlaw** (compte personnel, sans organisation). Le dépôt est sous AGPL-3.0-or-later ([LICENSE](LICENSE)). Les décisions structurantes sont consignées en ADR ([`docs/adr/`](docs/adr/README.md)). Le développement est mené par des agents d'IA orchestrés par le mainteneur : voir [AI_POLICY.md](AI_POLICY.md).

## Facteur bus

Le facteur bus est de **1**. Mesures : tout est documenté dans le dépôt, les dépendances critiques sont répertoriées dans `docs/dependencies.md` avec leur repli, les composants sont remplaçables (ADR-0026).

## Successeur

**Successeur : à désigner.** Il recevra l'accès au dépôt, à l'image GHCR, aux clés de signature et aux noms de registre (PyPI, npm). Désignation attendue avant la 1.0 ; la règle de passation figure dans [`docs/governance/portes.md`](docs/governance/portes.md) (>= 5 PR fusionnées en 6 mois : proposition d'un rôle de co-mainteneur).

## Portes, vélocité, arrêt

Les décisions de continuer, réduire ou arrêter suivent les portes P0–P3 et la règle d'arrêt de `docs/governance/portes.md`. Le suivi d'effort est dans `docs/governance/velocite.md`, le registre de risques dans `docs/governance/pre-mortem.md`.

## Notifications légales et modération

- Accusé de réception sous **72 h**.
- Pas de contre-notification sans avis juridique.
- Pas de réécriture d'historique.
- Les issues qui citent un site ou un outil non autorisé, ou renvoient à du contenu non autorisé, sont modérées (suppression des liens).

Canal : signalement privé GitHub (voir [SECURITY.md](SECURITY.md)).

## Continuité en cas de retrait

Si l'hébergeur retire le dépôt ou l'image : bascule vers le second registre d'images et le miroir du dépôt (palier M1b, [ADR-0029](docs/adr/0029-resilience-retrait-forge.md)).

## Archivage propre

En cas d'arrêt : mode « maintenance seule » (correctifs de sécurité 12 mois, statut en première ligne du README), puis archivage avec dernière release et **export intégral documenté** des données ([ADR-0026](docs/adr/0026-gouvernance-versions-perennite.md)).

## Triage et financement

Triage au mieux (voir la politique de support de [CONTRIBUTING.md](CONTRIBUTING.md)). **Aucun don** n'est accepté avant l'existence d'un hôte fiscal (Open Collective Europe ou association loi 1901). Les fonctions payantes sont exclues du projet. Toute structure porteuse impose une revue de `docs/legal/cra.md`.

## Revue de chevauchement

À chaque jalon, les exigences restantes sont comparées aux notes de version de Kavita, Komga et Suwayomi ; ce qu'ils livrent est retiré du périmètre.

## Sécurité du compte

Authentification à deux facteurs matérielle sur le compte du mainteneur. Ruleset sur `main` : PR obligatoire, `ci-success` requis, historique linéaire, squash, pas de force-push ni de suppression. Tags `v*` : ni suppression ni mise à jour.
