---
status: accepté (porteur, 2026-11-04)
date: 2026-11-04
decision-makers: porteur
---
# ADR-0019 — Nom du projet : Shelfhold

## Contexte et problème
Le nom de travail « Liber » servait à la fois au dépôt GitHub, à l'image GHCR, au paquet Python, au futur SDK publié et au préfixe des variables d'environnement. La revue de passe 1 (OBJ-LEG-08, arbitrage A0) a constaté le 2026-11-02 des collisions : le nom est pris sur PyPI et npm, un compte GitHub existe, et « Liber3 » désigne déjà un moteur de livres numériques sur IPFS. Le nom français « Rayonnage » avait alors été retenu. Le 2026-11-04, le porteur a demandé un **nom anglais**, plus courant dans l'écosystème des outils auto-hébergés. Les identifiants sont répétés dans le dépôt, l'image, le SDK et les variables : renommer après la publication du SDK (M6) casserait les plugins tiers.

## Facteurs de décision
- Disponibilité simultanée sur PyPI, npm, GitHub (compte ou organisation) et GHCR.
- Absence de produit voisin homonyme (livres, bibliothèques, lecteurs).
- Nom anglais, court, évocateur du rangement, sans marque évidente.
- Coût de renommage : nul avant le premier commit public, élevé après M6.

## Options considérées
Huit noms ont été comparés : Shelfstead, Shelfhold, Tomehaven, Spinestead, Leafstead, Tomewell, Spinery et Folioist.
- **Spinery** : écarté, l'organisation GitHub est prise.
- **Shelfhold** : libre partout (voir Décision) ; « shelf » dit le rangement, « hold » dit la garde et la conservation ; cohérent avec le vocabulaire UI « Étagère » ; distinctif.
- **Shelfstead, Tomehaven, Spinestead, Leafstead, Tomewell, Folioist** : disponibles mais moins directs. Les suffixes « -stead » et « -haven » évoquent un lieu plutôt qu'un atelier de rangement.

Écartés d'emblée car déjà pris : Shelfmark (outil voisin, calibrain/shelfmark), ShelfHaven, Shelfwright, HearthShelf, Tomestead, Lectern, Bindery, BookNest.

Anciennes options : « Liber » (écarté, collisions), « Rayonnage » (retenu en passe 1, remplacé à la demande du porteur).

## Décision
**Shelfhold** (« shelf » + « hold »). Vérifications du 2026-11-04 :
- PyPI `shelfhold` et `shelfhold-sdk` libres ; npm libre ; compte ou organisation GitHub `shelfhold` libre ;
- `shelfhold.com` enregistré le 2026-07-19 (page de parking) ; `.dev`, `.org` et `.app` libres ;
- aucune marque trouvée par recherche web (pas de recherche formelle EUIPO ou USPTO).

Identifiants :
- paquet Python `shelfhold` ;
- SDK interne `shelfhold.sdk`, puis paquet PyPI `shelfhold-sdk` en M6 ;
- dépôt et image `ghcr.io/<propriétaire>/shelfhold` (compte personnel du porteur au départ ; chemin défini à un seul endroit pour migrer vers une organisation, ADR-0026) ;
- variables `SHELFHOLD_*` ;
- CLI `shelfhold` ;
- point d'entrée de plugins `shelfhold.plugins` ;
- base `shelfhold.db` ;
- dossier réservé `.shelfhold/`.

Réservation des noms PyPI et npm (scope `@shelfhold` si un client TS est publié) dès M0. Le porteur a décidé de publier sans relecture juridique préalable (Q27a levée) : aucune recherche formelle de marque n'est donc planifiée avant le premier commit public.

## Conséquences
- Positives : aucune collision connue. Tous les livrables utilisent un seul nom, anglais, cohérent avec les identifiants de code. Le vocabulaire UI (« Étagère ») reste en français.
- Négatives : le renommage de tous les documents, exemples de compose et fixtures a été refait (passe 1, puis 2026-11-04). Un nom anglais dans un projet documenté d'abord en français demande une phrase d'explication dans le README. `shelfhold.com` est détenu par un tiers.

## Risque principal
Une marque antérieure apparaît, ou un tiers prend un nom de registre avant la réservation. Mitigation : réservation dès M0. Le risque de marque est accepté et consigné (`06-points-discutables.md`, risques résiduels ; Q27a levée).

## Réversibilité
Haute jusqu'au premier commit public (M0). Moyenne jusqu'à M6 (image GHCR, variables d'environnement et composes des premiers utilisateurs à renommer). Faible après la publication du SDK.

## Exigences liées
ENF-LEG-004, ENF-GOV-001, ENF-DOC-001.

## Sources
- https://pypi.org/project/liber/ et https://www.npmjs.com/package/liber (noms pris) ; https://github.com/liber (compte existant)
- https://pypi.org/project/shelfhold/ et https://pypi.org/project/shelfhold-sdk/ (absents au 2026-11-04)
- https://www.npmjs.com/package/shelfhold (absent au 2026-11-04)
- https://github.com/shelfhold (absent au 2026-11-04)
- https://github.com/calibrain/shelfmark (outil voisin, Shelfmark écarté)
- `shelfhold.com` : enregistré le 2026-07-19, page de parking
- `deliverables/review/ARBITRAGES-passe1.md` (A0), `deliverables/review/contradicteur-ops-secu-legal.md` (OBJ-LEG-08)
