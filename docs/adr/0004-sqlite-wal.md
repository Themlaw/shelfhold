---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 1)
decision-makers: mainteneur
---
# ADR-0004 — Persistance : SQLite WAL + SQLAlchemy 2 + Alembic, schéma portable Postgres

> **Amendée le 2026-11-02 (passe 1).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Charge d'écriture faible (un foyer + jobs), un seul hôte, budget RAM serré, et pas d'envie d'ajouter un conteneur de base. Il faut aussi une recherche plein texte gérant le CJK, des sauvegardes simples et une sortie possible vers Postgres.

## Facteurs de décision
- Zéro conteneur supplémentaire, RAM faible.
- Migrations fiables (ALTER limités sous SQLite).
- Recherche FTS incluant le CJK.
- Sauvegarde/restauration triviales.
- Réversibilité vers Postgres.

## Options considérées
### A. SQLite WAL (`synchronous=NORMAL`, `busy_timeout=5000`, un écrivain + pool lecteurs), SQLAlchemy 2.1, Alembic `render_as_batch=True`, FTS5 trigram
- Avantages : zéro ops ; Kavita, Komga et Audiobookshelf tournent sur SQLite à cette échelle ; FTS5 intégré ; `VACUUM INTO` et Litestream pour les sauvegardes.
- Inconvénients : un seul écrivain, donc contention possible lors d'imports massifs ; ALTER limités (mode batch d'Alembic).
### B. PostgreSQL (+ Procrastinate pour les jobs)
- Avantages : concurrence d'écriture, file de jobs éprouvée.
- Inconvénients : un conteneur de plus, ~100 Mo de RAM (ordre de grandeur), sauvegardes plus lourdes pour un homelab.
### C. Recherche externe (Meilisearch)
- Avantages : pertinence.
- Inconvénients : un second processus de plusieurs centaines de Mo ; rejeté. tantivy-py en processus reste la voie de secours (chemin pris par Paperless).

## Décision
Option A (D3). Schéma écrit pour rester portable (pas de SQL spécifique hors adaptateurs) ; migrations jouées sur Postgres en nightly. Sauvegarde : `VACUUM INTO` nocturne, et Litestream en option. Migrations automatiques et idempotentes au démarrage, précédées d'une sauvegarde.

## Conséquences
- Positives : déploiement minimal, RAM faible, sauvegarde par simple copie de fichier.
- Négatives : écritures sérialisées ; transactions courtes obligatoires ; certaines migrations recréent des tables.

## Risque principal
Contention d'écriture qui fige l'UI pendant les gros imports (R6). Mitigation : tâche écrivain unique, lots courts, benchmark ENF-PERF-007 ; Postgres reste une bascule de configuration.

## Réversibilité
Haute : l'ORM et le schéma sont portables, et le passage à Postgres est testé en nightly (ENF-PORT-002).

## Amendement passe 1 (2026-11-02)
- **Postgres en nightly retiré** (A16, SIM-14) : ENF-PORT-002 est retirée. La portabilité reste une règle de revue (« pas de SQL spécifique hors adaptateurs »), sans pipeline dédié ; la bascule vers Postgres n'est plus « testée en continu » et la réversibilité passe de haute à **moyenne**.
- **Migrations** (A24) : plus de migration implicite silencieuse. Commande `shelfhold migrate` (utilisable comme service `depends_on: service_completed_successfully`), sauvegarde pré-migration nommée par révision et hors rotation, **refus de démarrer sur un schéma plus récent** que le code, aucune migration dans une release patch (ENF-OPS-002).
- **File de jobs** : base Huey dans un fichier séparé `huey.db` (ADR-0005) pour isoler les verrous d'écriture.
- **Recherche** (A33) : FTS5 `unicode61 remove_diacritics 2` + table trigram + repli `LIKE` sous 3 caractères CJK (EF-BIB-010).
- **Sauvegarde** : `VACUUM INTO` vers une destination distincte, avec ≥ 2× la taille de la base libre (ENF-OPS-006) ; détails dans ADR-0023.
- **Profil de référence** (ENF-PERF-008) : toutes les mesures de perf portent sur 2 000 Œuvres / 60 000 Unités / 3 M pages.

## Amendement passe 3 (2026-11-02)
- **Entretien** (C13, ENF-OPS-016) : `PRAGMA optimize` quotidien ; `wal_autocheckpoint` et checkpoint `TRUNCATE` nocturne (un lecteur SSE long ne doit pas bloquer la troncature) ; `incremental_vacuum`/`VACUUM` mensuel dans la fenêtre de maintenance, base Huey comprise ; rétentions chiffrées (événements et jobs 90 j, audit 1 an, ENF-OPS-009).
- **Unicité sans NULL** (GAP-3-25) : SQLite tient deux NULL pour distincts dans un `UNIQUE` ; les clés d'unicité (Édition notamment) n'ont aucune colonne nullable (valeurs sentinelles `''`/`unknown`).

## Exigences liées
EF-BIB-010, EF-INT-015, EF-INT-016, ENF-OPS-001, ENF-OPS-002, ENF-OPS-006, ENF-PERF-005, ENF-PERF-007, ENF-PERF-008, ENF-OPS-009, ENF-OPS-016, EF-BIB-001.

## Sources
- https://github.com/benbjohnson/litestream
- https://github.com/quickwit-oss/tantivy-py
- https://github.com/procrastinate-org/procrastinate
- https://github.com/Kareadita/Kavita
- https://github.com/gotson/komga
- https://www.sqlite.org/fts5.html
- https://www.sqlite.org/lang_createindex.html#unique_indexes *[mem]*
- https://www.sqlite.org/wal.html#ckpt *[mem]*
- `deliverables/review/chasseur-trous-passe3.md` (GAP-3-17, GAP-3-25)
