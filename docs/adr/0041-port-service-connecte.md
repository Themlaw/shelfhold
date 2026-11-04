---
status: accepté
date: 2026-11-03
amended: 2026-11-03 (passe 7) ; 2026-11-03 (passe 10) ; 2026-11-03 (passe 11) ; 2026-11-03 (passe 12)
decision-makers: mainteneur
---
# ADR-0041 — Port `ConnectedService`

## Contexte et problème
ADR-0008 range les services connectés (Suwayomi, lncrawl, serveurs OPDS) dans le contrat Source. Or beaucoup de règles du cœur sont propres à Suwayomi (GAP-6-09) : catégories « exclue du téléchargement automatique », file de téléchargement côté serveur et `download_request`, état « téléchargé » avant le GET, purge ou retrait de revendication (`ui_login`), relevé puis restauration des réglages amont (`detach --restore-upstream`), sonde « extension chargée », mode d'authentification, réveil. lncrawl (M2) a lui aussi des tâches asynchrones côté serveur (GAP-6-15), et les outils de la liste de veille pourraient en faire autant. Sans port explicite, M2 ajoutera un deuxième jeu de cas particuliers ou une refonte. Par ailleurs, Suwayomi retire son API REST historique (#2512) et ajoute un CSRF en authentification Basic (#2513) (U7).

## Facteurs de décision
- Le cœur ne doit dépendre d'aucun service tiers nommé.
- Un deuxième service connecté (lncrawl en M2) sans nouveau cas particulier.
- Contrat additif dans `api` v1 (ADR-0024).
- Vérifiable par une règle de frontière et un faux service.

## Options considérées
### A. Laisser les règles Suwayomi dans le cœur (situation passe 5)
- Avantages : aucun travail de plus en M1a.
- Inconvénients : couplage ; lncrawl recopie ou contourne ; tests de frontière impossibles.
### B. Port `ConnectedService` au-dessus de `Source`, capacités facultatives déclarées
- Avantages : le cœur programme contre des capacités ; un service sans catégories ou sans file reste utilisable ; frontière vérifiable.
- Inconvénients : une couche d'abstraction de plus, définie avec un seul exemple concret en M1a.
### C. Un plugin par service avec sa propre orchestration
- Avantages : liberté totale de chaque adaptateur.
- Inconvénients : les garanties de sûreté (purge, mise à l'abri, retour arrière) seraient réimplémentées par plugin.

## Décision
Option B (arbitrages F9, F12 ; EF-SRC-033).
- Capacités facultatives : `categories` (suivi par catégorie, exclusion du téléchargement automatique), `server_queue` (`request(unit) → ticket`, `poll(ticket)`, `fetch(ticket)`, `cancel(ticket)`, ticket persisté dans `download_request`, générique pour Suwayomi et lncrawl), `release_claim` (revendication et purge, retrait de la revendication en `ui_login`), `upstream_restore` (relevé et restauration des réglages amont), `health_probe`, `auth_mode`, `wake` (délégué au WakeHook, ADR-0025 amendée).
- Le cœur ne programme que contre ces capacités ; une capacité absente désactive la fonction correspondante avec un message clair.
- **Règle import-linter** : aucun symbole `suwayomi` ou `lncrawl` hors de son adaptateur (ADR-0003 amendée).
- **Test** : un faux service connecté minimal, sans catégories, passe le cycle de suivi complet.
- **Suwayomi (U7)** : l'adaptateur n'envoie que du GraphQL en `application/json` (le CSRF de #2513 ne vise que les requêtes de type formulaire) ; un 401 « CSRF mismatch » est classé `failure_class = version`, jamais `auth` ; il n'appelle que les routes REST conservées par #2512 (`chapter/{id}/download` en GET et HEAD, route de page, vignettes), liste blanche vérifiée par un test de contrat (spike S5 puis nightly).

## Conséquences
- Positives : lncrawl et un futur service se branchent sans toucher au cœur ; les gardes intouchables (purge, mise à l'abri, retour arrière) sont écrites une seule fois.
- Négatives : la forme des capacités est fixée avec un seul service réel ; elle sera relue à l'arrivée de lncrawl (M2), par ajout seulement.

## Risque principal
Une capacité mal découpée qui oblige lncrawl à un contournement. Mitigation : capacités facultatives et additives, revue du port à l'entrée de M2, faux service dans le kit de conformité.

## Réversibilité
Haute : port interne, capacités additives.

## Amendement passe 7 (2026-11-03)
G5, G7 (GAP-7-05, GAP-7-07).
- `server_queue` : cible polymorphe `UnitRef | RangeRef` ; une ligne `download_request` par ticket et `download_request_unit` pour les Unités couvertes ; relance et plafond comptés en Unités ; la réponse produit un seul `export_item` pour une plage (EF-SRC-034). Le faux plugin de conformité de M1a déclare ensemble `ranged_content` et `server_queue`.
- `upstream_restore` écrit dans la table dédiée `upstream_snapshot`, sauvegardée et jamais purgée sans détachement (invariant I31) ; le journal d'intentions n'en garde que l'opération de restauration.

## Amendement passe 10 (2026-11-03)
K3 (GAP-10-03, GAP-10-11).
- Règle du port : les identités rendues (`remote_unit_id`, `SeriesRef`) sont celles de la **source amont**, stables à la réinstallation de l'outil connecté ; un identifiant de la base de l'outil n'est qu'un `fetch_handle` en cache.
- Capacité facultative **`INSTANCE_FINGERPRINT`** (M1b) : une empreinte changée met le service en pause (`source.instance_changed`) sans la cascade « série retirée » (EF-SRC-027), purge les tickets orphelins, puis `shelfhold source reconnect` ré-résout par identité stable et réactive les liaisons dont la série revient.

## Amendement passe 11 (2026-11-03)
L3, L6 (GAP-11-04, Z1). Le Suwayomi des tests de contrat n'a que la source locale et des CBZ synthétiques, sans dépôt d'extensions ni réseau sortant (ENF-LEG-013). Une source dont le résolveur DNS du foyer renvoie `0.0.0.0` ou `::` est signalée `source.dns_blocked`, distincte d'une panne ou d'une attaque SSRF (EF-UX-003).

## Exigences liées
EF-SRC-033, EF-SRC-032, EF-SRC-008, EF-SRC-009, EF-SRC-026, EF-SRC-031, EF-BIB-038, EF-EXP-056, EF-INT-022, EF-SRC-034, EF-SRC-036, ENF-LEG-013, EF-UX-003.

## Sources
- `deliverables/review/chasseur-trous-passe6.md` (GAP-6-09, GAP-6-15, GAP-6-18)
- `deliverables/review/explorateur-passe6.md` (U7)
- `deliverables/review/ARBITRAGES-passe6.md` (F9, F12)
- https://github.com/Suwayomi/Suwayomi-Server/pull/2512
- https://github.com/Suwayomi/Suwayomi-Server/pull/2513
- https://github.com/seddonym/import-linter
- `deliverables/review/chasseur-trous-passe7.md`, `deliverables/review/explorateur-passe7.md`, `deliverables/review/ARBITRAGES-passe7.md` (passe 7)
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)

## Amendement passe 12 (2026-11-03)
M7 (GAP-12-07, GAP-12-10).

- `release_claim` n'est appelée par `purge --now` qu'à partir de **M1b** ; en M1a, la seule écriture amont reste `detach --restore-upstream` : la purge retire la revendication côté Shelfhold et alerte « supprimer dans Suwayomi ».
- Pour jouer la vraie file de téléchargement (`server_queue`, état « téléchargé », route CBZ) sans contenu tiers, une **extension de test synthétique** construite dans le dépôt pointe vers un serveur HTTP de fixtures interne au job, installée depuis le fichier construit (jamais depuis un dépôt d'extensions), en nightly dès M1a (ENF-LEG-013).

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)

## Amendement passe 13 (2026-11-03)
N5 (GAP-13-09).

- En M1a, la purge d'une Unité ferme côté Shelfhold ses tickets `download_request` ouverts ; à partir de M1b, elle appelle `cancel(ticket)`. La migration de M1b liste les copies amont laissées par les purges de M1a et propose `release_claim` ou `unfollow`, après confirmation de l'admin.
- Tests : `test_purge_m1a_closes_download_tickets_shelfhold_side`, `test_m1b_migration_offers_release_of_m1a_purged_upstream_copies`.

Source : `deliverables/review/chasseur-trous-passe13.md`, `deliverables/review/explorateur-passe13.md`, `deliverables/review/ARBITRAGES-passe13.md` (passe 13)
