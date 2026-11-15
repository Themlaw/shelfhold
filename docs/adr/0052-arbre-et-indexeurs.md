---
status: accepté
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
  - le **manifeste de fichiers** : une ligne d'`export_record` par `export_item` et par liaison (clé inchangée `(shelf_destination_id, export_item_id)`, ADR-0011 passe 6), dont l'état du **fichier** (chemin, `file_hash`, `planned`/`written`/`missing`/`recycled`) fait autorité sur la ligne de la liaison **primaire** ; les lignes des liaisons secondaires en portent une **copie**, écrite dans la **même transaction** que la ligne primaire (le chemin relatif reste la clé de résolution de chaque indexeur, EF-EXP-039), plus l'état propre à leur indexeur (`remote_ref`, `scanned`/`resolved`, `error`). Une divergence entre la copie et la ligne primaire est une erreur d'intégrité (T9), jamais un second état du fichier ;
  - un **rendu unique** (`render_profile`, voir plus bas), qui produit les mêmes octets pour tous les indexeurs : `file_hash` est donc **par arbre** et non plus par liaison (précise ADR-0011 passe 7 et ADR-0043).
- **Indexeur** (`tree_indexer`) : une liaison Étagère × Destination (`shelf_destination`) rattachée à un arbre, avec un **rôle** `primary` ou `secondary`. Un indexeur apporte ce qui ne touche pas aux octets : scan et scan ciblé, API d'inventaire et de résolution, confirmation d'arrivée, progression de lecture, métadonnées poussées par API (âge verrouillé, ADR-0027), audit d'accès (ADR-0027), `access_partitioning`, compte de service. Il **n'écrit jamais** dans l'arbre : les écritures sont celles de l'hôte (plan `FsOp`, ADR-0024), une seule fois par fichier.
  - `primary` : la liaison qui a créé l'arbre ; elle fixe `root_subpath` et le gabarit d'origine, et c'est son `render()` qui produit le plan. L'hôte valide ce plan contre le **profil de l'arbre** avant de l'exécuter (avec `check_destination_plan`) : il retire les `write` d'un sidecar interdit par un indexeur de l'arbre (opération journalisée, déterministe) et refuse le plan, avec `target.tree_render_conflict`, s'il reste une violation du profil (nom, disposition, format). Une primaire écrite pour 1.3 ignore donc tout des secondaires sans risque pour elles. Exactement une primaire par arbre.
  - `secondary` : toute liaison rattachée ensuite.
- **Cycle de vie.** Un arbre a **toujours** au moins un indexeur (la primaire). Détacher une secondaire (ADR-0034) restaure ses seuls réglages d'avant adoption et ne touche à aucun fichier. Le retrait d'une secondaire (EF-EXP-031) est toujours un détachement : l'option « tout retirer » ne supprime aucun fichier de l'arbre. Détacher ou retirer la primaire exige d'abord de promouvoir une secondaire (opération explicite) ou de détacher les secondaires ; sinon refus `target.tree_has_indexers` (409, admin), avec la liste des indexeurs. La **promotion** échange les rôles sans recopier ni réécrire quoi que ce soit : `render_profile` et chemins restent figés (I22) ; elle est refusée si la secondaire ne sait pas produire un plan conforme au profil (`target.tree_render_conflict`). Détacher la dernière libère l'arbre : la ligne `tree` disparaît avec sa dernière liaison, les fichiers restent, le miroir suit les règles d'ADR-0046 (`rapatrier` comme aujourd'hui).
- Les transports `dropbox`, `push` et `pull` n'ont **pas d'arbre** : ils n'ont ni dossier géré, ni sentinelle, ni rendu partageable. La règle « une destination `tree` co-liée sert de source d'octets » (EF-BIB-045, GAP-7-09) compte une liaison indexeuse comme co-liée. Le dossier d'ingestion d'une boîte de dépôt (CWA, EF-EXP-022) ne peut ni contenir un arbre ni être contenu dans un arbre (`target.tree_overlap`) : ses fichiers y seraient vus comme ceux d'un autre écrivain, et le consommateur de la boîte les y supprimerait.
- Le **dossier natif** (EF-EXP-010, L07 de M4) est une destination `tree` propriétaire de son arbre (`local/<Série>/Ch.… .cbz`) ; il ne déclare pas `indexes_tree` en M4 et n'accueille pas d'indexeur secondaire (sa disposition n'est pas celle du rendu commun). Partager un arbre entre le dossier natif et un serveur est **Plus tard** ; d'ici là, le fan-out les sert par deux arbres (option B). La garde de boucle contre la source locale de Suwayomi (L07) est un contrôle distinct, avec son propre code.

### Invariants
- **T1 (identité)** : un arbre est unique par `(root_id, root_subpath)` normalisé ; c'est la seule identité **persistée**. À la création d'une liaison, deux chemins qui désignent le même dossier (même chemin réel, ou même `(st_dev, st_ino)`, montage lié) sont **le même arbre** : le chemin demandé est ramené à la forme canonique de l'arbre existant. `(st_dev, st_ino)` ne sert qu'à ce contrôle et n'est jamais stocké comme identité (`st_dev` change d'un démarrage à l'autre, EF-EXP-043) ; un lien symbolique dans le chemin reste refusé (`RESOLVE_NO_SYMLINKS`, EF-EXP-006).
- **T2 (une étagère)** : tous les indexeurs d'un arbre appartiennent à la **même Étagère**, donc au même plafond d'âge, aux mêmes profils autorisés et à la même rétention. Deux Étagères sur le même arbre restent refusées.
- **T3 (un écrivain)** : l'hôte seul écrit, sous bail de racine, une fois par fichier ; un indexeur déclare ne pas écrire (`indexes_tree`, plus bas). Un changement de fichier non produit par l'hôte reste un « autre écrivain » (EF-EXP-043), quel que soit l'indexeur qui le signale ; les opérations d'un indexeur sur ses propres caches ne sont pas des écritures de l'arbre.
- **T4 (rendu unique)** : le rendu est calculé pour l'arbre, pas pour la liaison. Il est recalculé et revalidé contre **tous** les indexeurs à chaque rattachement et à chaque changement de gabarit ; il reste figé par fichier (I22).
- **T5 (exclusions)** : `.shelfhold/` est exclu **de chaque indexeur** (Kavita : `ExcludePatterns` ; Komga : exclusions de dossiers de la bibliothèque), et le test « jamais indexé » porte sur chaque indexeur.
- **T6 (bibliothèques distantes dédiées)** : la bibliothèque distante de chaque indexeur est incluse dans l'arbre et ne contient rien d'autre (`shelf.library_not_dedicated`, `target.remote_folder_shared` inchangés) ; deux indexeurs de **deux arbres** ne couvrent pas le même dossier distant.
- **T7 (indépendance des indexeurs)** : l'état, la santé, les événements, le curseur de progression et les comptes sont **par indexeur** ; un serveur injoignable suspend sa propre résolution et la progression qui le concerne, jamais l'écriture ni les autres indexeurs. Deux exceptions, qui mettent en pause l'écriture de **tout l'arbre** : un motif d'accès (T10) et un autre écrivain (T3, la pause porte sur l'arbre ou sur la série, une seule fois, quel que soit l'indexeur qui le signale).
- **T8 (mêmes octets, mêmes noms)** : un rattachement qui laisserait un fichier déjà rangé sans satisfaire les contraintes du nouvel indexeur est refusé (voir « Rattachement »).
- **T9 (un état de fichier)** : pour un arbre et un `export_item`, toutes les lignes d'`export_record` des indexeurs portent le même chemin, le même `file_hash` et le même état de fichier que la ligne primaire. La vérification du rangement (EF-EXP-018) et la détection d'un autre écrivain (EF-EXP-043) tournent **une fois par arbre**, pas une fois par indexeur. Une même instance de destination n'indexe un arbre qu'une fois (au plus un indexeur par `(tree_id, instance de destination)`).
- **T10 (accès : le plus faible des indexeurs décide)** : écrire un fichier dans l'arbre, c'est le livrer à **tous** ses indexeurs, y compris à celui dont la résolution est suspendue (un serveur continue de scanner seul). Donc : `access_partitioning` est vérifié pour **chaque** indexeur, au rattachement et à chaque planification (EF-EXP-047, L06a) ; l'audit d'exposition (EF-EXP-055, EF-EXP-036 ; Komga en L08b) passe pour l'indexeur **avant** son rattachement, puis est rejoué chaque jour comme pour la primaire, aucun indexeur n'étant moins audité qu'elle ; un `target.child_exposure` ou une version `known_bad_versions` de catégorie `access_control` sur **un** indexeur met en pause l'écriture de **tout l'arbre** (ADR-0027, I43), pas seulement la résolution de cet indexeur. Un enfant ne lit donc jamais par Komga ce que Kavita lui cache : soit les deux serveurs le cachent, soit rien n'est écrit. Les métadonnées poussées par API (âge de série verrouillé, ADR-0027 ; `update_metadata`) le sont **par indexeur**.
- **T11 (une copie)** : un arbre est **une** copie, quel que soit le nombre de ses indexeurs. `independent_copies()` (EF-BIB-045), `irreplaceable`, la purge Suwayomi (EF-BIB-038) et la page Santé comptent par arbre ; `present_in(edition, binding)` reste par liaison.

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
- la destination ne déclare pas `indexes_tree`, ou l'un des indexeurs (primaire comprise) ne déclare pas `series_grouping` et `name_normalizer` ;
- la même instance de destination indexe déjà l'arbre (T9) ;
- l'indexeur ne satisfait pas le cloisonnement de l'Étagère ou son audit d'exposition échoue (T10) → `target.child_exposure` ou le code de refus d'EF-EXP-047, comme pour une liaison neuve ;
- le rendu commun n'existe pas (intersection de formats vide, normaliseurs en conflit sur un nom déjà rangé, sidecar requis par l'un et interdit par l'autre, profil non plat) ;
- des fichiers **déjà rangés ou adoptés** ne satisfont pas l'indexeur (ex. sous-dossiers de tomes dans une série adoptée) : l'écart est listé par série, rien n'est réécrit.
Le dernier cas fait l'objet d'un **rapport de compatibilité** calculé sur le manifeste avant toute liaison ; il s'applique aussi à un arbre en `adopt_ro` (ADR-0034, ADR-0022 passe 6), où Shelfhold ne rend rien : seul le contrôle de compatibilité, en lecture, s'exécute.
Le code `target.tree_overlap` est **conservé** pour tout recouvrement qui n'est pas une identité : un dossier **contenu dans** un arbre ou qui **en contient** un (un `root_subpath` préfixe de l'autre, comparé **par composants** et non par préfixe de chaîne, sur la même racine physique, ou un dossier dont le `(st_dev, st_ino)` est celui d'un ancêtre ou d'un descendant d'un arbre), y compris le dossier d'ingestion d'une boîte de dépôt. Il l'est aussi pour une identité d'arbre sans demande explicite de rattachement. Il ne se lève jamais pour un recouvrement partiel. Deux codes sont ajoutés (409, admin) : `target.tree_render_conflict` pour l'absence de rendu commun, `target.tree_has_indexers` pour le retrait d'une primaire qui a encore des secondaires.

### Journal d'intentions, verrous et plans de renommages
- Une opération sur un fichier de l'arbre est **une** intention `fs_intent` (EF-EXP-041, ADR-0031), sous bail de la racine, quel que soit le nombre d'indexeurs ; elle ne se duplique pas par liaison. Les verrous de ressource et le refus des plans périmés (EF-EXP-042) portent sur l'arbre : deux indexeurs ne planifient jamais chacun une opération sur le même fichier.
- Un **plan de renommages** (L29 de M3, ADR-0040 ; migration de liaison de L16 de M4, à l'intérieur d'une même racine) se calcule **une fois par arbre** : collision de série vérifiée sous le normaliseur de **chaque** indexeur, avertissement qui nomme chaque indexeur touché et, pour chacun, la préservation de la progression de chaque membre (jetons par membre, B15). Après l'exécution, chaque indexeur ré-résout par chemin (EF-EXP-039) et reçoit son propre scan (scan de dossier pour Kavita, scan de bibliothèque débouncé pour Komga, EF-EXP-020). Une intention ouverte rejouée au démarrage déclenche la ré-résolution de tous les indexeurs de l'arbre.

### Miroir de provenance
Le miroir reste par racine physique, au format 1.2 inchangé : une ligne par fichier (chemin relatif + `file_hash`), jamais une par indexeur. Son descripteur de liaison (GAP-9-03) est celui de la **primaire**. Les références distantes des secondaires sont un cache ré-résolu par chemin (EF-EXP-039) et ne sont pas dans le miroir. Après une perte de `/data`, la réadoption reconstruit l'arbre et sa primaire ; l'admin rattache de nouveau les secondaires (même contrôle de compatibilité, pas de réécriture).

### Contrat de destination v1.4 (ajouts compatibles N-1)
Voir les amendements d'ADR-0024 et d'ADR-0011. En résumé, `TargetCapabilities` gagne :
- `indexes_tree: bool = False` : la destination accepte d'être rattachée en `secondary` à un arbre qu'elle n'a pas créé (elle n'y écrit rien) ; Kavita et Komga first-party la déclarent. Défaut `False` : une destination écrite pour 1.3 reste propriétaire de son arbre, rien ne change pour elle ;
- `forbidden_sidecars: frozenset[str] = frozenset()` : noms de sidecars dont la présence nuit à la destination (Kavita : `series.json`) ; sérialisé en liste triée, pour un aller-retour JSON stable (ADR-0024 passe 7).
`series_grouping` et `name_normalizer` existent déjà ; la valeur de `series_grouping` devient décisive pour le rendu commun. Les membres propres à Kavita (`parse_name_call`, `ensure_library_call`, `resolve_remote_calls`, `event_subscription`, `progress_calls`) restent `@deprecated` depuis 1.1, **tous présents** jusqu'au SDK 2.0 (M6, avec le kit publié, D7) : M4 n'en retire aucun. Le second adaptateur (Komga) sert à concevoir leur remplaçant générique, ajouté à côté.

### Progression et comptes
La progression, les comptes de service ou membres et les alias de document (ADR-0045, `document_alias`) sont **par liaison**, donc par indexeur. Chaque indexeur a son curseur de synchronisation. La fusion de deux progressions concurrentes d'un même membre (lu dans Kavita puis dans Komga) se règle sur `reading_position` (ADR-0045) ; la règle de fusion appartient au lot de synchronisation de progression, pas à cet ADR.

### Migration
Pour chaque liaison `tree` existante (M1a à M3, une destination par Étagère), **y compris en `adopt_ro`**, la migration de M4 crée un `tree` (racine, `root_subpath` normalisé, profil de rendu tiré du gabarit copié et des capacités) et un `tree_indexer` de rôle `primary`. Migration en avant sans perte (ADR-0039), idempotente ; aucune écriture sur disque ni appel à une destination. Si deux liaisons existantes tombent sur la même identité après normalisation (ce que le refus de la passe 8 devait empêcher), la migration **s'arrête** avec la liste des liaisons en cause : jamais de fusion silencieuse en un arbre. Les liaisons `dropbox`, `push` et `pull` n'ont pas d'arbre.

## Conséquences
- Positives : Kavita et Komga se rattachent au même dossier sans double écriture ni « autre écrivain » croisé ; la famille voit une bibliothèque ; progression et audit d'accès restent par serveur ; aucun membre du SDK n'est retiré en M4.
- Négatives : le rendu est contraint par le plus strict des indexeurs (profil plat à deux niveaux, pas de `series.json` avec Kavita) ; un arbre adopté avec une disposition imbriquée ne peut pas accueillir Komga ; deux niveaux d'état à lire (arbre, indexeur) dans la page de santé.
- Pour le schéma de L02 : `tree(id, root_id, root_subpath, render_profile)` (le plan de M4 omet **`root_subpath`**), avec `UNIQUE (root_id, root_subpath)` (T1) ; `tree_indexer(tree_id, shelf_destination_id, role)` avec `UNIQUE (shelf_destination_id)` (une liaison, un arbre), un index unique partiel `UNIQUE (tree_id) WHERE role = 'primary'` (une primaire), au plus un indexeur par instance de destination et par arbre (T9), et l'égalité d'Étagère des indexeurs d'un arbre vérifiée par invariant (T2).
- Pour les lots de fan-out (L06a, L06b) : « chaque liaison a son `file_hash` » et « rendu refait par destination » se lisent **par arbre** ; deux liaisons sur deux arbres gardent deux rendus.

## Risque principal
Un rendu commun qui convient à Kavita et Komga sur le papier et diverge à l'usage (un serveur lit un champ ou une règle de nom différente d'une version à l'autre). Mitigation : normaliseurs versionnés et `known_bad_versions` par destination ; test de contrat nightly sur les deux serveurs contre le même arbre (série par nom, série par dossier) ; écart constaté au scan signalé et jamais « corrigé » par réécriture ; repli par deux arbres (option B) toujours disponible.

## Réversibilité
Haute pour le disque : l'arbre est ce que M1a à M3 écrivent déjà ; détacher les secondaires ramène à l'état d'avant, sans toucher aux fichiers. Moyenne pour le schéma : `tree` et `tree_indexer` s'ajoutent par migration en avant ; les retirer demanderait de rendre à chaque liaison son propre rendu. Les ajouts au SDK sont facultatifs et à défaut sûr : ne pas les utiliser est toujours permis.

## Exigences liées
EF-EXP-058, EF-EXP-010, EF-EXP-020, EF-EXP-022, EF-EXP-031, EF-EXP-032, EF-EXP-036, EF-EXP-041, EF-EXP-042, EF-EXP-043, EF-EXP-029, EF-EXP-039, EF-EXP-047, EF-EXP-055, EF-EXP-059, EF-EXP-065, EF-LEC-014, EF-BIB-038, EF-BIB-045, ENF-QUAL-005, I22, I43, ADR-0011, ADR-0022, ADR-0024, ADR-0027, ADR-0031, ADR-0034, ADR-0040, ADR-0043, ADR-0045, ADR-0046.

## Tests attendus (écrits en rouge par les lots de M4)
`test_second_indexer_attaches_to_existing_tree`, `test_tree_identity_same_folder_by_inode`, `test_overlap_without_identity_still_refused` (`target.tree_overlap`), `test_indexers_of_a_tree_belong_to_one_shelf`, `test_common_render_satisfies_name_and_folder_grouping`, `test_series_json_never_written_in_tree_with_kavita`, `test_attach_refused_with_reasons_when_no_common_render`, `test_attach_refused_for_adopted_nested_layout`, `test_unreachable_indexer_does_not_pause_writes`, `test_kit_indexes_tree_defaults_false`, `test_migration_creates_primary_tree_per_tree_binding`, `test_migration_stops_on_tree_identity_collision`, `test_child_exposure_on_any_indexer_pauses_tree_writes`, `test_attach_runs_exposure_audit_of_the_new_indexer`, `test_host_drops_forbidden_sidecar_from_primary_plan`, `test_secondary_record_mirrors_primary_file_state`, `test_foreign_writer_detected_once_per_tree`, `test_tree_counts_as_one_independent_copy`, `test_dropbox_folder_inside_tree_refused`, `test_remove_primary_with_secondaries_refused`, `test_remove_secondary_never_deletes_files`, `test_rename_plan_once_per_tree_rescans_every_indexer`, `test_same_instance_indexes_a_tree_once`.

## Objections du contradicteur (relecture adverse, 2026-11-15)
Relecture adverse de l'architecture avant acceptation. Chaque objection est **corrigée** dans le texte ci-dessus, sauf mention.

### Bloquant
- **B1. Un enfant lisait par Komga ce que Kavita lui cache.** T7 ne mettait en pause que la résolution d'un indexeur en défaut, et l'écriture « pour un motif d'accès qui touche l'Étagère ». Or écrire dans l'arbre, c'est livrer à tous ses indexeurs : un `child_exposure` côté Komga laissait Kavita écrire, et Komga indexer. Rien ne disait non plus que l'audit de l'indexeur secondaire passe avant son rattachement. **Corrigé** : T10 (le plus faible des indexeurs décide, pause de tout l'arbre, audit avant rattachement puis quotidien, cloisonnement par indexeur), refus au rattachement, exception explicite dans T7.
- **B2. Deux indexeurs comptés comme deux copies.** Rien n'empêchait `independent_copies()` de compter deux liaisons sur un même arbre comme deux exemplaires, ce qui autorise une purge Suwayomi ou retire `irreplaceable` à tort (EF-BIB-038, EF-BIB-045) : perte de données. **Corrigé** : T11.

### Majeur
- **M1. Qui rend le plan ?** Le rendu était « par arbre », mais le plan `FsOp` vient du `render()` d'un plugin, qui ignore les autres indexeurs (une primaire Komga écrirait `series.json` sous Kavita). **Corrigé** : la primaire rend, l'hôte valide contre le profil de l'arbre, retire les sidecars interdits et refuse le reste.
- **M2. Deux états de fichier.** Les lignes secondaires sans chemin cassaient la résolution par chemin (EF-EXP-039) ; avec un chemin, rien ne garantissait la cohérence, et la vérification du rangement ou la détection d'un autre écrivain auraient tourné, et alerté, une fois par indexeur. **Corrigé** : copie écrite dans la même transaction, T9, contrôles une fois par arbre.
- **M3. Journal d'intentions et plans de renommages absents.** **Corrigé** : section dédiée (une intention par opération, verrous par arbre, plan calculé une fois par arbre sous tous les normaliseurs, re-scan de chaque indexeur).
- **M4. Cycle de vie incomplet.** « Tout retirer » (EF-EXP-031) sur une secondaire aurait supprimé les fichiers de l'arbre ; le retrait de la primaire et la promotion n'étaient pas définis. **Corrigé** : `target.tree_has_indexers`, promotion sans réécriture, retrait d'une secondaire = détachement.
- **M5. Couverture des destinations de M4.** Le dossier d'ingestion de CWA pouvait se trouver dans un arbre (fichiers pris pour un autre écrivain, puis supprimés par CWA) ; le dossier natif n'avait pas de statut. **Corrigé** : recouvrement boîte de dépôt / arbre refusé ; dossier natif propriétaire de son arbre, sans indexeur secondaire en M4 (partage Plus tard).
- **M6. Miroir de provenance.** Le descripteur de liaison de la ligne (GAP-9-03) n'était pas défini avec plusieurs indexeurs. **Corrigé** : celui de la primaire ; format 1.2 inchangé ; secondaires rattachées de nouveau après une perte de `/data`.

### Mineur
- **m1. Identité par inode.** `st_dev` change d'un démarrage à l'autre : il ne peut pas être une identité persistée. **Corrigé** dans T1 ; comparaison des chemins par composants.
- **m2. Même instance deux fois sur un arbre.** **Corrigé** : T9 et contrainte de L02.
- **m3. Migration.** Liaisons `adopt_ro` oubliées, collision après normalisation non traitée. **Corrigé**.
- **m4. `set[str]`** n'a pas d'aller-retour JSON stable et sert de défaut mutable. **Corrigé** : `frozenset[str]`, liste triée.
- **m5. Schéma de L02 et lots de fan-out.** La carte L02 du plan omet `root_subpath` ; L06a et L06b parlent de `file_hash` et de rendu « par destination ». **Corrigé** ici (Conséquences) ; le plan de M4, hors dépôt, est à aligner.
- **m6. Simplicité.** Supprimer les rôles (indexeurs symétriques, état de fichier porté par l'arbre) serait plus simple sur le papier, mais changerait la clé du manifeste de la passe 6 et tous les lecteurs d'`export_record` en M4. **Non retenu** : la primaire reste, réduite à « qui rend le plan et porte l'état faisant autorité » ; la promotion est un simple échange de rôles. Option B (deux arbres) reste le repli.
- **m7. Fusion de progressions concurrentes** (Kavita puis Komga) : renvoyée au lot de synchronisation de progression, comme le texte le dit. **Non bloquant.**

Verdict : plus rien de bloquant après corrections ; ADR acceptée.

## Sources
- https://wiki.kavitareader.com/guides/scanner/managefiles/
- https://wiki.kavitareader.com/guides/scanner/
- https://wiki.kavitareader.com/guides/metadata/comics/
- https://komga.org/docs/guides/scan-analysis-refresh
- https://github.com/anansi-project/comicinfo
- `deliverables/review/chasseur-trous-passe8.md` (GAP-8-09, GAP-8-10), `deliverables/review/ARBITRAGES-passe8.md` (H3)
- `docs/spec/06-points-discutables.md` (Q48)
