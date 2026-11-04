**Statut du projet : actif — pré-alpha, M0** (dernière revue de porte : aucune pour l'instant ; la porte P0 clôt M0, voir `docs/governance/portes.md`).

# Shelfhold

> **English summary.** Shelfhold is a self-hosted workshop for a household's personal digital library (books, comics, manga), built to sit next to [Kavita](https://www.kavitareader.com/). It finds, files, normalises and exports legally obtained or free content into the libraries you already run. It is a workshop, not a reader and not a sharing service. Legal sources only; protected files are not supported. The project is pre-alpha (milestone M0: repository, CI, skeleton). Most of the code is written by AI agents orchestrated by the maintainer, under mandatory TDD and cross-review: see [AI_POLICY.md](AI_POLICY.md). License: AGPL-3.0-or-later with a provisional plugin exception; the SDK is MIT. Documentation is French first.

## Ce que c'est

**Shelfhold est l'atelier de la bibliothèque personnelle de votre foyer, pour Kavita.** Il s'installe à côté de Kavita (et de vos autres outils de lecture) et s'occupe de ce que ceux-ci ne font pas : récupérer les contenus que vous avez le droit de conserver, les ranger sous une identité stable, les normaliser (noms, numérotation, métadonnées), puis les exporter vers la bibliothèque de lecture du foyer, avec contrôle parental par étagère.

Le nom est anglais (« shelf » + « hold », garder sur l'étagère) ; le vocabulaire de l'interface reste français (« Étagère », « Atelier »). Voir [ADR-0019](docs/adr/0019-nom-du-projet.md).

### Ce que le projet fait

- Suit des séries depuis des sources légales et des services connectés que **vous** configurez, puis range les chapitres et volumes dans une bibliothèque Kavita existante, sans la réécrire.
- Garde une copie canonique par unité (livre, tome, chapitre), avec sauvegarde et export ouverts.
- Fonctionne sans interface (configuration par fichier et variables d'environnement), puis avec une interface web d'atelier.
- Reste local : aucune télémétrie, aucun service central, aucun modèle d'IA embarqué.

### Ce que le projet ne fait pas

- Il ne fournit ni contenu, ni source de contenu non autorisée, ni index d'extensions préconfiguré : les méta-sources sont livrées **sans aucune source active**.
- Il ne contourne aucune protection technique et n'en explique aucun moyen.
- Il n'est pas un lecteur concurrent de Kavita (le lecteur intégré est secondaire et tardif), ni un service de partage ou d'hébergement pour des tiers.
- Aucune fonction payante, aucun lien d'affiliation.

### Posture légale

Sources légales uniquement ; les fichiers protégés ne sont pas pris en charge. Détails : [ADR-0015](docs/adr/0015-posture-legale.md) (ceci n'est pas un avis juridique).

### Code écrit par des agents

Le code est majoritairement écrit par des agents d'IA pilotés par le mainteneur, avec TDD obligatoire et relecture croisée. Lisez [AI_POLICY.md](AI_POLICY.md).

## Où en est-on

Pré-alpha. Le jalon M0 pose les fondations (dépôt, CI, image, squelette, spikes). Rien n'est encore utilisable au quotidien. Plan et jalons : [ROADMAP.md](ROADMAP.md). Aucune promotion publique avant 1.0.

Une image de développement sera publiée sous `ghcr.io/themlaw/shelfhold` (tag `edge`, puis `0.x` à partir des releases).

## Documentation

| Public | Où |
|---|---|
| Comprendre le projet | [`docs/spec/01-cahier-des-charges.md`](docs/spec/01-cahier-des-charges.md) |
| Architecture | [`docs/spec/02-architecture.md`](docs/spec/02-architecture.md) |
| Feuille de route et méthode TDD | [`docs/spec/03-roadmap-tdd.md`](docs/spec/03-roadmap-tdd.md) |
| Structure du dépôt, CI, releases | [`docs/spec/04-structure-repo.md`](docs/spec/04-structure-repo.md) |
| Décisions | [`docs/adr/`](docs/adr/README.md) |
| Gouvernance | [GOVERNANCE.md](GOVERNANCE.md), [`docs/governance/`](docs/governance/portes.md) |
| Contribuer | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Sécurité | [SECURITY.md](SECURITY.md) |

## Licence

AGPL-3.0-or-later, avec une permission additionnelle **provisoire** pour les plugins (voir [LICENSE](LICENSE)). Le dossier `sdk/` est sous licence MIT. Marques : [TRADEMARKS.md](TRADEMARKS.md).
