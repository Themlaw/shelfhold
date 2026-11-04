---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 1) ; 2026-11-03 (passe 6)
decision-makers: mainteneur
---
# ADR-0003 — Monolithe modulaire hexagonal, frontières vérifiées

> **Amendée le 2026-11-02 (passe 1).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Un seul mainteneur, un seul conteneur, mais un domaine riche : catalogue, acquisition, stockage, export, lecture, identité, jobs, hôte de plugins. Il faut des frontières nettes, testables isolément, sans le coût opérationnel des microservices.

## Facteurs de décision
- Testabilité (ports mockables, tests de domaine sans I/O).
- Simplicité de déploiement (un processus).
- Frontières vérifiées par outil, pas par discipline.
- Base pour le CQRS léger (lecture bibliothèque/OPDS).

## Options considérées
### A. Monolithe modulaire, ports & adaptateurs par module, bus d'événements en processus + outbox
- Avantages : un processus, des frontières explicites (`public.py` par module), l'outbox fiabilise webhooks, trackers et notifications (D33) ; modèle déjà éprouvé par Komga (domain/infrastructure/interfaces) et Immich.
- Inconvénients : cérémonie (ports, facades) ; demande des contrats d'import pour ne pas s'éroder.
### B. Monolithe en couches classique (Django-like)
- Avantages : conventions connues, rapide au début.
- Inconvénients : couplage progressif entre domaines ; le domaine finit par importer l'ORM.
### C. Microservices / plusieurs conteneurs
- Avantages : isolation forte.
- Inconvénients : coût ops et RAM incompatibles avec O7 et un mainteneur unique.

## Décision
Option A (D2). Modules : `catalog`, `acquisition`, `library` (store), `export`, `reading`, `identity`, `jobs`, `plugin_host`, `interfaces`, plus `infrastructure/*` (adaptateurs). Les modules communiquent seulement via leur facade `public.py` ou par événements. import-linter applique en CI trois types de contrats : `layers` (interfaces → application → domain), `independence` entre modules et `forbidden` (le domaine n'importe pas sqlalchemy, fastapi ni curl_cffi).

## Conséquences
- Positives : tests unitaires rapides sur le domaine ; échange d'adaptateurs (FastAPI→Litestar, SQLite→Postgres) local ; les modules peuvent être extraits plus tard.
- Négatives : plus de fichiers ; les violations de frontière bloquent la CI, ce qui ralentit les prototypes.

## Risque principal
Érosion des frontières sous pression de livraison. Mitigation : import-linter bloquant (ENF-QUAL-002) et ADR obligatoire pour toute nouvelle dépendance inter-modules.

## Réversibilité
Haute : on peut assouplir les contrats ou fusionner des modules sans réécriture.

## Amendement passe 1 (2026-11-02)
- **import-linter réduit** (SIM-17, ENF-QUAL-002) : on garde un contrat `forbidden` (le domaine n'importe ni sqlalchemy, ni fastapi, ni httpx/curl_cffi), un contrat `independence` entre modules et un contrat garantissant que `safe_open_archive()` est l'unique point d'ouverture d'archives (ENF-SEC-002). Le contrat `layers` complet et l'**ADR obligatoire pour toute nouvelle dépendance inter-modules sont abandonnés** : une revue de PR suffit.
- **Bus d'événements** (A12) : en V1, bus en processus + table `event` append-only (alimente le SSE avec `Last-Event-ID`) + notifications Apprise ; l'**outbox persistée** n'arrive qu'avec les webhooks (M4, EF-INT-005). Chaque événement porte un `schema_version` (EVO-41).
- **Modules** : `library` stocke par Unité sans CAS en V1 (ADR-0010) ; `reading` n'est livré qu'en M5 (ADR-0012, ADR-0020) ; le SDK vit dans le monorepo (`shelfhold.sdk`) jusqu'à M6 (ADR-0024). Nouveau port `WakeHook` dans `integrations` (ADR-0025).

## Amendement passe 3 (2026-11-02)
- **Point d'entrée XML unique** (C12, ENF-SEC-014) : comme `safe_open_archive()`, `safe_parse_xml()` (`defusedxml`, ou lxml avec `resolve_entities=False`, `no_network=True`, `huge_tree=False`, plafonds de taille et de profondeur) est le seul moyen d'analyser du XML (OPF, ComicInfo, OPDS, RDF, SVG). Contrat import-linter : `xml.etree`, `xml.dom`, `xml.sax` et `lxml` interdits hors de `infrastructure/xml`.

## Amendement passe 4 (2026-11-03)
Texte amont (D12, GAP-4-11) : un second point d'entrée unique, `sanitize_upstream_text()` (nh3, liste blanche minimale), est imposé par un contrat import-linter, comme `safe_parse_xml` ; tout texte venu d'une source passe par lui à l'ingestion, avant l'affichage, les notifications, ComicInfo et l'OPF.

## Amendement passe 6 (2026-11-03)
F9, F6, F4, F10 (GAP-6-06, GAP-6-09).
- Nouveaux contrats import-linter : (1) aucun symbole `suwayomi` ou `lncrawl` hors de son adaptateur (`adapters/sources/suwayomi/`, `adapters/sources/lncrawl/`) ; le cœur ne connaît que le port `ConnectedService` (ADR-0041, EF-SRC-033) ; (2) aucune fonction d'écriture de `fsops` n'accepte une racine `adopt_ro` (EF-EXP-060, invariant I24), doublé d'un test ; (3) `independent_copies()` est l'unique point de calcul des exemplaires indépendants (EF-BIB-045) ; (4) `normalize_lang()` est l'unique constructeur de `LanguageTag` (ENF-I18N-005).

## Exigences liées
ENF-QUAL-002, EF-INT-005, ENF-OBS-003, ENF-SEC-002, ENF-SEC-014, ENF-SEC-015, EF-SRC-033, EF-EXP-060, EF-BIB-045, ENF-I18N-005.

## Sources
- https://github.com/seddonym/import-linter
- https://github.com/gotson/komga
- https://github.com/immich-app/immich
- https://github.com/paperless-ngx/paperless-ngx
- `deliverables/review/chasseur-trous-passe3.md` (GAP-3-12), `deliverables/review/ARBITRAGES-passe3.md` (C12)
- https://pypi.org/project/defusedxml/ *[mem]*
- `deliverables/review/chasseur-trous-passe4.md` (GAP-4-11)
- https://github.com/messense/nh3
- `deliverables/review/ARBITRAGES-passe4.md`
