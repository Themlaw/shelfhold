---
status: proposée
date: 2026-11-15
decision-makers: mainteneur
---
# ADR-0052 — Arbre et indexeurs : un arbre écrit une fois, lu par un ou plusieurs serveurs

## Contexte et problème
Cas courant d'un homelab : Kavita et Komga lisent le même dossier. Avec les contrats de M1a à M3, ce seraient deux liaisons `tree` sur les mêmes chemins. Chacune verrait les fichiers de l'autre comme ceux d'un « autre écrivain » (EF-EXP-043), et chacune voudrait son propre rendu (nom, ComicInfo, sidecars). La passe 8 (H3, GAP-8-10) a donc fait **refuser** deux liaisons `tree` aux racines qui se recouvrent, avec le code stable `target.tree_overlap` (EF-EXP-058), en attendant cette décision (Q48). Le refus est en vigueur dans le cœur (`check_binding`) ; il ne se lève qu'avec le présent ADR (préalable de M4, EF-EXP-058).

Le problème de fond : le contrat de la passe 6 confond deux choses. La **destination** sait écrire un arbre et le faire reconnaître (scan, API, progression). L'**arbre** est ce qui est sur le disque : un dossier, une sentinelle, un manifeste, un miroir de provenance, un rendu. Tant que les deux sont fusionnés, un second serveur sur le même dossier ne peut être qu'un second écrivain.

Les deux serveurs ne regroupent pas les séries de la même façon (GAP-8-09) : Kavita lit le **nom** de la série (ComicInfo, nom de fichier) ; Komga prend le **dossier**. Un rendu commun n'existe que s'il fait coïncider les deux.

## Facteurs de décision
- Un seul écrivain par fichier, un seul rendu, une seule sentinelle (ADR-0022), un seul miroir de provenance (ADR-0046).
- Fichiers figés (I22, ADR-0040) : un rendu ne se corrige pas après coup ; la compatibilité se vérifie **avant** la liaison.
- Compatibilité N-1 de l'API de plugin (ADR-0024) : ajouts seulement, aucun membre retiré avant le SDK 2.0 (D7 de M4).
- Cloisonnement d'accès et contrôle parental (ADR-0027) : un arbre ne mélange pas deux plafonds d'âge.
- Le schéma de M4 (L02 : `tree`, `tree_indexer`) en dépend : les entités et les invariants doivent être précis.

## Options considérées
### A. Séparer « arbre » et « indexeur » (recommandation de Q48)
- Avantages : un seul écrivain, un seul rendu ; colle au cas réel ; pas de double espace disque.
- Inconvénients : le rendu doit convenir à tous les indexeurs (Kavita par nom, Komga par dossier) ; concept nouveau dans le contrat.
### B. Deux arbres distincts, un par serveur
- Avantages : aucun concept nouveau.
- Inconvénients : double espace disque, deux rendus, la famille voit deux bibliothèques. Reste possible comme **repli** choisi par l'admin (fan-out : deux liaisons, deux arbres, deux dossiers).
### C. Komga en destination secondaire sans écriture, simple lecture de l'arbre Kavita
- Avantages : rien à écrire pour Komga.
- Inconvénients : pas de scan ciblé ni de préservation de progression pour Komga ; ni compatibilité vérifiée, ni audit d'accès.

## Décision
Option A (D6 de M4, recommandation de Q48 retenue).

### Entités
- **Arbre** (`tree`) : un dossier d'écriture de Shelfhold, identifié par **(racine physique, `root_subpath` normalisé)**. `tree.root_id` désigne la racine physique d'ADR-0022 (table `physical_root`) ; la **sentinelle**, le **bail** et la corbeille restent **par racine physique** (ADR-0022), donc partagés par tous les arbres d'une même racine ; le **miroir de provenance** reste par racine physique (ADR-0046, format 1.2 inchangé : il ne dépend pas du nombre d'indexeurs). L'arbre porte :
  - le **manifeste de fichiers** : une ligne d'`export_record` par `export_item` et par liaison (clé inchangée `(shelf_destination_id, export_item_id)`, ADR-0011 passe 6), dont l'état du **fichier** (chemin, `file_hash`, `planned`/`written`/`missing`/`recycled`) fait autorité sur la ligne de la liaison **primaire** ; les lignes des liaisons secondaires ne portent que l'état propre à leur indexeur (`remote_ref`, `scanned`/`resolved`, `error`) ;
  - un **rendu unique** (`render_profile`, voir plus bas), qui produit les mêmes octets pour tous les indexeurs : `file_hash` est donc **par arbre** et non plus par liaison (précise ADR-0011 passe 7 et ADR-0043).
- **Indexeur** (`tree_indexer`) : une liaison Étagère × Destination (`shelf_destination`) rattachée à un arbre, avec un **rôle** `primary` ou `secondary`. Un indexeur apporte ce qui ne touche pas aux octets : scan et scan ciblé, API d'inventaire et de résolution, confirmation d'arrivée, progression de lecture, audit d'accès (ADR-0027), `access_partitioning`, compte de service. Il **n'écrit jamais** dans l'arbre : les écritures sont celles de l'hôte (plan `FsOp`, ADR-0024), une seule fois par fichier.
  - `primary` : la liaison qui a créé l'arbre ; elle fixe `root_subpath` et le gabarit d'origine. Exactement une par arbre.
  - `secondary` : toute liaison rattachée ensuite.
- Un arbre a **toujours** au moins un indexeur (la primaire). Détacher une secondaire (ADR-0034) restaure ses seuls réglages d'avant adoption et ne touche à aucun fichier ; détacher la primaire exige d'abord de promouvoir une secondaire (opération explicite) ; détacher la dernière libère l'arbre (fichiers conservés, `rapatrier` comme aujourd'hui).
- Les transports `dropbox`, `push` et `pull` n'ont **pas d'arbre** : ils n'ont ni dossier, ni sentinelle, ni rendu partageable. La règle « une destination `tree` co-liée sert de source d'octets » (EF-BIB-045, GAP-7-09) compte une liaison indexeuse comme co-liée.

### Invariants
- **T1 (identité)** : un arbre est unique par `(root_id, root_subpath)` normalisé ; deux chemins qui désignent le même dossier (même chemin réel, ou même `(st_dev, st_ino)`, lien symbolique ou montage lié) sont **le même arbre**.
- **T2 (une étagère)** : tous les indexeurs d'un arbre appartiennent à la **même Étagère**, donc au même plafond d'âge, aux mêmes profils autorisés et à la même rétention. Deux Étagères sur le même arbre restent refusées.
- **T3 (un écrivain)** : l'hôte seul écrit, sous bail de racine, une fois par fichier ; un indexeur déclare ne pas écrire (`indexes_tree`, plus bas). Un changement de fichier non produit par l'hôte reste un « autre écrivain » (EF-EXP-043), quel que soit l'indexeur qui le signale ; les opérations d'un indexeur sur ses propres caches ne sont pas des écritures de l'arbre.
- **T4 (rendu unique)** : le rendu est calculé pour l'arbre, pas pour la liaison. Il est recalculé et revalidé contre **tous** les indexeurs à chaque rattachement et à chaque changement de gabarit ; il reste figé par fichier (I22).
- **T5 (exclusions)** : `.shelfhold/` est exclu **de chaque indexeur** (Kavita : `ExcludePatterns` ; Komga : exclusions de dossiers de la bibliothèque), et le test « jamais indexé » porte sur chaque indexeur.
- **T6 (bibliothèques distantes dédiées)** : la bibliothèque distante de chaque indexeur est incluse dans l'arbre et ne contient rien d'autre (`shelf.library_not_dedicated`, `target.remote_folder_shared` inchangés) ; deux indexeurs de **deux arbres** ne couvrent pas le même dossier distant.
- **T7 (indépendance des indexeurs)** : l'état, la santé, les événements, le curseur de progression et les comptes sont **par indexeur** ; un serveur injoignable suspend sa propre résolution et la progression qui le concerne, jamais l'écriture ni les autres indexeurs. L'écriture se met en pause seulement pour un motif d'accès qui touche l'Étagère (ADR-0027) ou pour un autre écrivain.
- **T8 (mêmes octets, mêmes noms)** : un rattachement qui laisserait un fichier déjà rangé sans satisfaire les contraintes du nouvel indexeur est refusé (voir « Rattachement »).

### Regroupement des séries (`series_grouping`) et rendu commun
Chaque indexeur déclare dans ses capacités `series_grouping ∈ {name, folder}` (défaut `name`, ADR-0011 passe 8) et son normaliseur de nom (`name_normalizer`, versionné). Le **profil de rendu** de l'arbre est celui qui satisfait **tous** les indexeurs :
- **Deux niveaux, séries feuilles** : `<racine>/{Série}/<fichier>`. Un dossier de série contient les fichiers de la série et aucun sous-dossier de livres : Komga ferait de chaque sous-dossier une série, Kavita les fusionnerait.
- **Nom = dossier** : pour chaque indexeur `name`, le nom que lit l'indexeur (ComicInfo `Series`, série de l'OPF ou nom de fichier, selon sa règle `parse`) est égal au nom du dossier ; le nom de fichier commence par le nom de série (`{Series}/{Series} - …`). Pour chaque indexeur `folder`, le dossier est une série et une seule. Les deux coïncident par construction ; une divergence constatée au scan est signalée (proposition de désambiguïsation), jamais corrigée par réécriture (fichiers figés).
- **Unicité sous tous les normaliseurs** : la détection de collision de série (EF-EXP-032, `export_series`) vaut sous le normaliseur de **chaque** indexeur de l'arbre ; deux séries qui ne se distinguent que sous l'un d'eux bloquent le plan.
- **Contraintes de nommage** : la plus stricte l'emporte (longueurs minimales, interdits en union, insensibilité à la casse si un seul indexeur l'est).
- **Formats** : `accepted_content_types` et `accepted_image_types` s'intersectent, dans l'ordre de l'indexeur primaire ; une intersection vide refuse la liaison.
- **Sidecars** : seuls ceux acceptés par tous les indexeurs sont écrits ; un sidecar qu'un indexeur déclare nuisible (`forbidden_sidecars`) l'exclut pour l'arbre entier. **`series.json` n'est jamais écrit dans un arbre dont un indexeur est Kavita** (D6 de M4) ; ComicInfo, lu par les deux serveurs, reste le canal commun.

### Rattachement et refus
La création d'une liaison dont le dossier **est** un arbre existant se fait par un choix explicite de l'admin (champ facultatif `tree_id` à la création, ajout compatible de l'API REST). Sans ce choix, elle est refusée comme aujourd'hui (`target.tree_overlap`, avec dans le détail l'arbre et la possibilité de rattacher).
Le rattachement est refusé, avec un code stable et les **raisons** structurées (`reasons[]` : indexeur, contrainte, valeur de chacun), dans ces cas :
- l'Étagère diffère (T2) → `target.tree_overlap` ;
- la destination ne déclare pas `indexes_tree` ;
- le rendu commun n'existe pas (intersection de formats vide, normaliseurs en conflit sur un nom déjà rangé, sidecar requis par l'un et interdit par l'autre, profil non plat) ;
- des fichiers **déjà rangés ou adoptés** ne satisfont pas l'indexeur (ex. sous-dossiers de tomes dans une série adoptée) : l'écart est listé par série, rien n'est réécrit.
Le dernier cas fait l'objet d'un **rapport de compatibilité** calculé sur le manifeste avant toute liaison ; il s'applique aussi à un arbre en `adopt_ro` (ADR-0034, ADR-0022 passe 6), où Shelfhold ne rend rien : seul le contrôle de compatibilité, en lecture, s'exécute.
Le code `target.tree_overlap` est **conservé** pour tout recouvrement qui n'est pas une identité : un dossier **contenu dans** un arbre ou qui **en contient** un (un `root_subpath` préfixe de l'autre sur la même racine physique, ou un dossier dont le `(st_dev, st_ino)` est celui d'un ancêtre ou d'un descendant d'un arbre). Il l'est aussi pour une identité d'arbre sans demande explicite de rattachement. Il ne se lève jamais pour un recouvrement partiel. Un code `target.tree_render_conflict` (409, admin) est ajouté pour l'absence de rendu commun.

### Contrat de destination v1.4 (ajouts compatibles N-1)
Voir les amendements d'ADR-0024 et d'ADR-0011. En résumé, `TargetCapabilities` gagne :
- `indexes_tree: bool = False` : la destination accepte d'être rattachée en `secondary` à un arbre qu'elle n'a pas créé (elle n'y écrit rien) ; Kavita et Komga first-party la déclarent. Défaut `False` : une destination écrite pour 1.3 reste propriétaire de son arbre, rien ne change pour elle ;
- `forbidden_sidecars: set[str] = set()` : sidecars dont la présence nuit à la destination (Kavita : `series.json`).
`series_grouping` et `name_normalizer` existent déjà ; la valeur de `series_grouping` devient décisive pour le rendu commun. Les membres propres à Kavita (`parse_name_call`, `ensure_library_call`, `resolve_remote_calls`, `event_subscription`, `progress_calls`) restent `@deprecated` depuis 1.1, **tous présents** jusqu'au SDK 2.0 (M6, avec le kit publié, D7) : M4 n'en retire aucun. Le second adaptateur (Komga) sert à concevoir leur remplaçant générique, ajouté à côté.

### Progression et comptes
La progression, les comptes de service ou membres et les alias de document (ADR-0045, `document_alias`) sont **par liaison**, donc par indexeur. Chaque indexeur a son curseur de synchronisation. La fusion de deux progressions concurrentes d'un même membre (lu dans Kavita puis dans Komga) se règle sur `reading_position` (ADR-0045) ; la règle de fusion appartient au lot de synchronisation de progression, pas à cet ADR.

### Migration
Pour chaque liaison `tree` existante (M1a à M3, une destination par Étagère), la migration de M4 crée un `tree` (racine, `root_subpath`, profil de rendu tiré du gabarit copié et des capacités) et un `tree_indexer` de rôle `primary`. Migration en avant sans perte (ADR-0039) ; aucune écriture sur disque. Les liaisons `dropbox`, `push` et `pull` n'ont pas d'arbre.

## Conséquences
- Positives : Kavita et Komga se rattachent au même dossier sans double écriture ni « autre écrivain » croisé ; la famille voit une bibliothèque ; progression et audit d'accès restent par serveur ; aucun membre du SDK n'est retiré en M4.
- Négatives : le rendu est contraint par le plus strict des indexeurs (profil plat à deux niveaux, pas de `series.json` avec Kavita) ; un arbre adopté avec une disposition imbriquée ne peut pas accueillir Komga ; deux niveaux d'état à lire (arbre, indexeur) dans la page de santé.
- Pour le schéma de L02 : `tree(id, root_id, render_profile)` doit aussi porter **`root_subpath`**, avec `UNIQUE (root_id, root_subpath)` (T1) ; `tree_indexer(tree_id, shelf_destination_id, role)` avec `UNIQUE (shelf_destination_id)` (une liaison, un arbre), un seul `primary` par arbre, et l'égalité d'Étagère des indexeurs d'un arbre vérifiée par invariant (T2).

## Risque principal
Un rendu commun qui convient à Kavita et Komga sur le papier et diverge à l'usage (un serveur lit un champ ou une règle de nom différente d'une version à l'autre). Mitigation : normaliseurs versionnés et `known_bad_versions` par destination ; test de contrat nightly sur les deux serveurs contre le même arbre (série par nom, série par dossier) ; écart constaté au scan signalé et jamais « corrigé » par réécriture ; repli par deux arbres (option B) toujours disponible.

## Réversibilité
Haute pour le disque : l'arbre est ce que M1a à M3 écrivent déjà ; détacher les secondaires ramène à l'état d'avant, sans toucher aux fichiers. Moyenne pour le schéma : `tree` et `tree_indexer` s'ajoutent par migration en avant ; les retirer demanderait de rendre à chaque liaison son propre rendu. Les ajouts au SDK sont facultatifs et à défaut sûr : ne pas les utiliser est toujours permis.

## Exigences liées
EF-EXP-058, EF-EXP-032, EF-EXP-043, EF-EXP-029, EF-EXP-039, EF-EXP-059, EF-EXP-065, EF-LEC-014, EF-BIB-045, ENF-QUAL-005, I22, ADR-0011, ADR-0022, ADR-0024, ADR-0027, ADR-0034, ADR-0040, ADR-0043, ADR-0045, ADR-0046.

## Tests attendus (écrits en rouge par les lots de M4)
`test_second_indexer_attaches_to_existing_tree`, `test_tree_identity_same_folder_by_inode`, `test_overlap_without_identity_still_refused` (`target.tree_overlap`), `test_indexers_of_a_tree_belong_to_one_shelf`, `test_common_render_satisfies_name_and_folder_grouping`, `test_series_json_never_written_in_tree_with_kavita`, `test_attach_refused_with_reasons_when_no_common_render`, `test_attach_refused_for_adopted_nested_layout`, `test_unreachable_indexer_does_not_pause_writes`, `test_kit_indexes_tree_defaults_false`, `test_migration_creates_primary_tree_per_tree_binding`.

## Sources
- https://wiki.kavitareader.com/guides/scanner/managefiles/
- https://wiki.kavitareader.com/guides/scanner/
- https://wiki.kavitareader.com/guides/metadata/comics/
- https://komga.org/docs/guides/scan-analysis-refresh
- https://github.com/anansi-project/comicinfo
- `deliverables/review/chasseur-trous-passe8.md` (GAP-8-09, GAP-8-10), `deliverables/review/ARBITRAGES-passe8.md` (H3)
- `docs/spec/06-points-discutables.md` (Q48)
