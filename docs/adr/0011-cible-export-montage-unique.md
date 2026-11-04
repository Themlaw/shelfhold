---
status: accepté, remplacé en partie par ADR-0022
date: 2026-11-02
amended: 2026-11-02 (passe 1) ; 2026-11-03 (passe 6) ; 2026-11-03 (passe 7) ; 2026-11-03 (passe 8) ; 2026-11-03 (passe 10) ; 2026-11-03 (passe 11) ; 2026-11-03 (passe 12)
decision-makers: mainteneur
---
# ADR-0011 — Contrat de destination d'export, Étagères et Destinations (montage : voir ADR-0022)

> **Amendée le 2026-11-02 (passe 1).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Douleur n°1 : les montages Suwayomi faits à la main pour Kavita (P1). Chaque serveur a ses règles. Kavita : une série par dossier, nom tiré du fichier ou de la ComicInfo, pas de champ `Manga`, « Vol 0 » réel, `.kavitaignore` supprimé en v0.7.11 au profit d'`ExcludePatterns`, scan sauté si la mtime du dossier n'a pas changé (précision à la minute). Komga : chaque dossier est une série, pas de scan par chemin, corbeille. Mihon local : supprime tout `*.json` d'un dossier de série. Il faut un mécanisme générique, pilotable depuis l'UI et sans montage manuel.

## Facteurs de décision
- Zéro action manuelle sur le FS (O1).
- Exports corrects par construction (O3), testables et rejouables (dry-run).
- Cycle de vie complet : idempotence, upgrade, suppression, réconciliation.
- Ajouter une cible sans toucher au cœur.

## Options considérées
### A. Les cibles **décrivent** (capabilities, gabarits, sidecars, ops API) et le **cœur exécute** les opérations fichiers ; Groupes → N Bindings ; un seul montage `/exports` au même chemin partout
- Avantages : plugins de cible purs et testables ; dry-run = même planificateur ; chemins identiques dans les appels API, sans traduction ; fan-out (Kavita + dossier natif).
- Inconvénients : contrat de capabilities riche à maintenir ; il faut ajouter le montage `/exports` dans le compose de Kavita.
### B. Exporteurs impératifs (chaque cible écrit ses fichiers)
- Avantages : rapide pour la première cible.
- Inconvénients : écriture atomique, manifeste et dry-run dupliqués et incohérents.
### C. Un montage par cible et par bibliothèque (approche manuelle actuelle, ou *arr « root folders » distincts)
- Avantages : familier.
- Inconvénients : c'est précisément la douleur P1 ; table de correspondance de chemins obligatoire ; pas de rename atomique entre montages (EXDEV).

## Décision
Option A (D15–D20).
- **Contrat** `ExportTarget` : capabilities (`media_kinds`, `formats`, `layout` tree|dropbox|push, `sidecars`, `path_scoped_scan`, `collections`, `reading_lists`, `progress`, `manage_libraries`, `naming_constraints`) et ops (`ensure_library`, `scan_path`, `resolve_remote`, `upsert_collection`, `upsert_reading_list`, `push/pull_progress`, `delete_item`, `health`, `validate_name`).
- **Pipeline** : plan → dry-run → staging → fsync/chown → rename → bump mtime → manifeste → scan débouncé → réconciliation.
- **Upgrades** au même chemin avec corbeille `.recycle`.
- **Aperçu de nommage** validé par `GET /api/plugin/parse` de Kavita.
- **Cibles V1** : Kavita + dossier natif (convention Mihon local + ComicInfo v2.0 + champs du brouillon 2.1 (correction passe 2, B16) + EPUB OPF calibre et EPUB3). V2 : Komga, CWA, Calibre, Kobo, Kindle, Booklore/Grimmory, Stump.
- Table de correspondance de chemins en secours.

## Conséquences
- Positives : douleur P1 supprimée ; corrections Kavita centralisées (specials `SP`+`Specials/`, NFC, 255 octets, parenthèses, `LocalizedSeries`, `Manga=YesAndRightToLeft` pour Komga, ExcludePatterns).
- Négatives : chaque cible demande un test d'intégration avec un serveur réel (nightly) ; Kavita doit monter `/exports`.

## Risque principal
Évolution des règles de Kavita (R5). Mitigation : validation `plugin/parse`, Kavita épinglé en nightly. Plusieurs points de 06-export sont marqués [mem] et restent à vérifier : rafraîchissement ComicInfo sans changement de mtime, conservation de l'ID chapitre sur chemin stable, SSE Komga, disposition ABS, règles Stump.

## Réversibilité
Moyenne : le format sur disque engage les serveurs cibles (IDs, progression) ; changer un gabarit déclenche des renommages planifiés, gérés par le pipeline.

## Amendement passe 1 (2026-11-02)
- **Montage : remplacé par ADR-0022** (A20, A21, A25). On ne demande plus d'ajouter `/exports` au compose de Kavita : Shelfhold monte le **parent existant** des médias Kavita (ex. `/srv/media/kavita:/library`) et applique une **table de correspondance de chemins** par défaut (`/library/manga↔/manga`, `/library/books↔/books`, `/library/comics↔/comics`). « Même chemin partout » devient une option. Staging et corbeille vivent dans le même montage (`/library/.shelfhold/`, contrainte EXDEV). Pas de chown : uid/gid du conteneur + umask configurable. Fichier **sentinelle** `.shelfhold-mount-id` (EF-EXP-029).
- **Vocabulaire** (A30) : « Groupe d'export » devient **Étagère** (`Shelf`) et « Cible/Binding » devient **Destination** (`ExportTarget`, liaison `ShelfDestination`). Une étagère → une destination en M1 (Kavita), fan-out en M4 ; étagère choisie **par Édition** (A31).
- **Destinations V1 = Kavita seule** (A2, A3). Dossier natif/Mihon-local, Komga, CWA en **M4** ; Kobo en M6 ; Calibre, Kindle, Booklore/Grimmory, Stump, Audiobookshelf « Plus tard » (EF-EXP-035). Génération EPUB3 maison retirée (EF-EXP-012, passthrough seulement).
- **Adoption** de la bibliothèque Kavita existante en M1 (EF-EXP-028) ; collision de nom de série **détectée au plan** via `plugin/parse` + inventaire (EF-EXP-032) ; ré-étagement = **plan de renommages** avec préservation de la progression (EF-EXP-030).
- **Secrets** (A28) : le cœur détient la clé admin Kavita, jamais transmise au plugin de destination ; ops reçoivent `f` et `cfg` explicites (EF-EXP-001).
- **Points [mem] levés** (closure-A) : Komga n'a pas de scan par dossier (`POST /api/v1/libraries/{id}/scan` seulement) ; confirmation d'arrivée par événements Kavita SignalR (`hubs/messages`, `ScanSeries`/`SeriesAdded`) ou Komga SSE (`/sse/v1/events`), avec délai max et repli polling (EF-EXP-034) ; règles Stump et Audiobookshelf relevées (EF-EXP-027, EF-EXP-035).

## Amendement passe 3 (2026-11-02)
- **Capacité `access_partitioning`** (C11, EF-EXP-047) : `none` | `library` | `user` dans `TargetCapabilities`. Une étagère dont le classement maximal est < 18+ ne peut viser qu'une destination `library` ou `user` ; sinon la liaison est refusée (pas de simple confirmation). Kavita et Komga déclarent `library` ; une boîte de dépôt ou un dossier natif déclarent `none`.
- **`accepted_image_types`** (C17, EF-EXP-046) : la destination déclare les formats d'image acceptés pour les couvertures ; Kavita : JPEG, PNG, WebP (pas AVIF ni JXL en couverture).

## Amendement passe 6 (2026-11-03)
F3 (GAP-6-03, GAP-6-10, GAP-6-11).
- Le contrat ExportTarget déclare son **transport** : `tree` (arbre de fichiers : Kavita, dossier, Komga), `dropbox` (boîte de dépôt qui consomme le fichier), `push` (envoi par API ou e-mail, avec `idempotency_key`), `pull` (le client vient chercher : OPDS sortant, émulation de synchronisation) ; et `can_update`, `can_delete`, `confirms_arrival`, `reconcilable`, `access_partitioning`, **`egress: foyer | externe`**, `accepted_content_types` ordonné (EF-EXP-058).
- Le **manifeste** est clé par `(shelf_destination_id, export_item_id)`, avec un `locator` typé selon le transport (`path`, `remote_id`, `idempotency_key`, aucun) ; pour `tree`, le chemin relatif reste la clé de **résolution** (EF-EXP-039).
- Vérification du rangement, détection d'un autre écrivain et sentinelle ne s'appliquent qu'au transport `tree`.
- Un fichier `watermarked` est refusé par le planificateur vers toute destination `egress = externe`, sans passe-droit (F3, ADR-0033 amendée).
- Kavita est `tree`, `foyer`. L'opération M4 `replace(old_records[], new_record)` de l'assemblage par tome s'exprime désormais sur `export_item` et `unit_coverage` (ADR-0040).

## Amendement passe 7 (2026-11-03)
G10 (GAP-7-09, GAP-7-10).
- **`egress` par instance** : la valeur déclarée par le plugin n'est qu'un défaut (`externe` pour tout ce qui n'est pas un serveur de lecture du foyer) ; l'admin l'atteste sur l'instance de destination (`shelf_destination.egress`), par exemple pour un dossier natif synchronisé vers un nuage (EF-EXP-058, ENF-LEG-009).
- **Destinations `pull`** (OPDS et kosync en M5, Kobo en M6) : une étagère en mode passage n'y est liée que s'il existe une copie conservée ou une destination `tree` co-liée qui sert de source d'octets ; sinon la liaison est refusée avec un code stable.
- Le rendu (ComicInfo, OPF) est fait par destination à partir du canonique ; `file_hash` est propre à chaque `(shelf_destination, export_item)` (ADR-0043).

## Amendement passe 8 (2026-11-03)
H3 (GAP-8-09, GAP-8-10, GAP-8-12).
- Deux liaisons `tree` dont les racines se recouvrent sont refusées (`target.tree_overlap`) jusqu'à l'ADR « arbre et indexeurs » (Kavita et Komga indexant le même arbre), à écrire **avant M4** (Q48).
- Capacité `series_grouping ∈ {name, folder}` et normaliseur de nom par type de destination (M4) : l'unicité par nom normalisé et `plugin/parse` sont des règles de la destination Kavita.
- Les opérations fichier d'une destination sont un plan sérialisable `list[FsOp]` exécuté par l'hôte (ADR-0024 amendée).

## Amendement passe 10 (2026-11-03)
K9 (GAP-10-08, GAP-10-10), vu par un auteur de destination tierce.
- `FsOp.op` gagne `unlink` (suppression définitive) **réservé à l'hôte** pour `purge --now` ; un plan rendu par une destination qui le contient est refusé. `TargetCapabilities.can_purge` distingue la suppression définitive de la corbeille propre à la destination (`can_delete`).
- `readback ∈ {none, verify, fetch}` (défaut `none`) dit si un exemplaire en destination peut servir de source d'octets (EF-BIB-045) ou seulement de copie vérifiable.
- Port `render()` pur et `write_inline` à taille bornée pour les fichiers annexes générés (`series.json`, `metadata.json`) ; espaces de noms `plugin.<clé>.*` pour les événements, codes et `waiting_state` d'un plugin.
- `access_partitioning` déclaré par un plugin **tiers** est un défaut proposé, attesté par l'admin par instance (comme `egress`) et audité quand la destination le permet.
- Forme fixée avant le gel des `FsOp` et la doc auteur (M3) ; kit destinations en M4.

## Amendement passe 11 (2026-11-03)
GAP-11-10, M4. Un `write` d'un `FsOp` qui désigne un contenu par référence n'est accepté que si la référence est un `export_item` **planifié pour cet appel et cette liaison** ; l'hôte refuse toute autre référence (`test_fsop_write_ref_must_be_planned_for_binding`), ce qui empêche une destination tierce d'écrire un contenu adulte dans une racine famille ou un fichier tatoué vers `egress = externe`.

## Exigences liées
EF-EXP-001, EF-EXP-002, EF-EXP-003, EF-EXP-004, EF-EXP-005, EF-EXP-006, EF-EXP-007, EF-EXP-008, EF-EXP-009, EF-EXP-018, EF-EXP-028, EF-EXP-029, EF-EXP-030, EF-EXP-032, EF-EXP-034, EF-INT-005, EF-EXP-046, EF-EXP-047, EF-EXP-058, EF-EXP-061, EF-BIB-044, ENF-LEG-009, EF-EXP-058, EF-EXP-032, EF-EXP-065.

## Sources
- https://wiki.kavitareader.com/guides/scanner/managefiles/
- https://wiki.kavitareader.com/guides/scanner/
- https://wiki.kavitareader.com/guides/metadata/comics/
- https://wiki.kavitareader.com/guides/metadata/epubs/
- https://komga.org/docs/guides/scan-analysis-refresh
- https://github.com/Suwayomi/Suwayomi-Server/wiki/Local-Source
- https://mihon.app/docs/guides/local-source/
- https://github.com/anansi-project/comicinfo
- https://wiki.servarr.com/readarr/settings
- https://casvt.github.io/Kapowarr/settings/mediamanagement/
- https://github.com/crocodilestick/Calibre-Web-Automated
- https://github.com/Kareadita/Kavita/blob/develop/Kavita.Models/DTOs/SignalR/MessageFactory.cs
- https://github.com/gotson/komga/blob/master/komga/src/main/kotlin/org/gotson/komga/interfaces/sse/SseController.kt
- https://github.com/gotson/komga/blob/master/komga/src/main/kotlin/org/gotson/komga/interfaces/api/rest/LibraryController.kt
- https://github.com/Kareadita/Kavita/issues/1744
- https://github.com/Kareadita/Kavita/issues/4747
- `deliverables/review/chasseur-trous-passe3.md` (GAP-3-11)
- `deliverables/review/chasseur-trous-passe7.md`, `deliverables/review/explorateur-passe7.md`, `deliverables/review/ARBITRAGES-passe7.md` (passe 7)
- `deliverables/review/chasseur-trous-passe8.md`, `deliverables/review/explorateur-passe8.md`, `deliverables/review/ARBITRAGES-passe8.md` (passe 8)
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)

## Amendement passe 12 (2026-11-03)
M10 (GAP-12-11, GAP-12-12, GAP-12-13).

- **Édition sans étagère** (`unshelved`) : non exportée, visible dans la vue « À ranger » ; source native ou import : copie gardée dans `/store` au format reçu ; service connecté : rien n'est récupéré avant le choix d'une étagère.
- **Collections et listes Kavita** (M4) : créées sous le compte de service Kavita de Shelfhold puis promues ; visibilité = accès aux bibliothèques et classement de Kavita ; liste propre à un profil promue seulement si toutes ses séries sont dans des bibliothèques ouvertes à ce profil.
- **`update_metadata`** : capacité facultative des destinations (ADR-0024) ; Kavita first-party l'implémente (âge de série verrouillé, ADR-0027).

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)

## Amendement passe 13 (2026-11-03)
N6 (AB1, AB2, AB3, AB5).

- **`push` exécutable (AB1)** : `ApiCall.body` facultatif, `JsonBody | ContentBody(content_ref, encoding = raw | multipart(fields, file_field))` ; un corps de contenu obéit aux règles de `FsOp.write` (référence à l'`export_item` planifié pour cet appel et cette liaison, `watermarked` refusé vers `egress = externe`, miroir de provenance jamais envoyé, hôte figé par la configuration, chemin relatif seulement). Tant qu'aucune destination first-party n'est en `push`, le kit refuse cette valeur (réservée jusqu'à M4). Un envoi par e-mail (`MailCall`) s'ajoutera de la même façon.
- **Calibre (AB2)** : EF-EXP-023 passe par les routes HTTP `/cdb/*` du serveur de contenu (`add-book` à corps brut, `add_duplicates = n` comme idempotence, `book_id` en `locator` ; `set-fields` sans verrou, `lock` ignoré et déclaré ; `delete-books`), jamais par un sous-processus `calibredb` ni un binaire calibre dans l'image ; protocole interne, donc plage de versions étroite et nightly épinglé.
- **Audiobookshelf (AB3, AB5)** : arbre de fichiers et scan ciblé seulement, jamais `POST /api/upload` (aucun scan, erreurs masquées) ; `known_bad_versions` < 2.32.2 (CVE-2026-42883, `access_control`).
- **Autres destinations (AB3)** : Komga (`update_metadata` champ par champ avec ses verrous), CWA et Grimmory/BookOrbit (`dropbox`), OPDS sortant (`pull`) s'expriment sans changement.
- Tests : `test_apicall_content_body_obeys_fsop_reference_and_egress_rules`, `test_kit_refuses_push_transport_before_m4`.

Source : `deliverables/review/chasseur-trous-passe13.md`, `deliverables/review/explorateur-passe13.md`, `deliverables/review/ARBITRAGES-passe13.md` (passe 13)
