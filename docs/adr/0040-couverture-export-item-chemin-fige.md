---
status: accepté
date: 2026-11-03
amended: 2026-11-03 (passe 7) ; 2026-11-03 (passe 8)
decision-makers: mainteneur
---
# ADR-0040 — Couverture des Unités, `export_item` et chemin exporté figé

## Contexte et problème
Jusqu'à la passe 5, le modèle rattachait un fichier à **une** Unité : `content.unit_id`, `export_record.unit_id` (ou `volume_id`), manifeste clé par chemin relatif. La passe 6 (GAP-6-01, GAP-6-03, GAP-6-07, GAP-6-19 ; explorateur U1) montre que cette relation 1-1 est fausse dès M1a :
- une bibliothèque manga adoptée mêle des **fichiers de tome** (`v01`, chapitres 1 à 8 dedans) et des **fichiers de chapitre** ; Kavita ne déduplique pas un tome et les chapitres qu'il contient ; avec un filigrane `follow_from` calculé sur « le plus haut numéro adopté », le suivi redemande des dizaines de chapitres déjà présents dans les tomes, et `backfill --gaps` les croit manquants ;
- lncrawl produit un EPUB par tome ou par plage de chapitres ; un tome `rolling_tail` (EF-BIB-040) grossit sous un nom stable ; l'assemblage webtoon par tome (M4) fusionne N Unités ; une intégrale ou un recueil couvre plusieurs tomes, voire plusieurs Œuvres ;
- les destinations V2 ne sont pas toutes des arbres de fichiers (boîte de dépôt, push, pull) : un manifeste clé par chemin ne les décrit pas ;
- un tome attribué tardivement à un chapitre, un special ou un titre corrigé changeraient le nom d'un fichier déjà lu si le gabarit en dépend : renommages de masse, progression Kavita et position KOReader perdues.
Tout cela est écrit en M1a (manifeste, intentions, purge, `irreplaceable`, filigrane). Le changer après coup est une migration de sens, pas une table de plus.

## Facteurs de décision
- Aucun doublon visible dans Kavita (attente centrale du pont) et aucun téléchargement inutile.
- Une seule relation générale plutôt que trois crochets (`covers[]` M4, `content_coverage` V2, lien 1-1).
- Les noms écrits en M1a sont une décision à sens unique pour les fichiers déjà lus.
- Coût nul ou presque en M1a : tables d'association, clés, invariants testés par propriétés.

## Options considérées
### A. Garder la relation 1-1 et ajouter `covers[]` en M4 (situation passe 5)
- Avantages : aucun travail en M1a.
- Inconvénients : doublons tome/chapitres dès l'adoption ; filigrane ambigu ; recalcul de la présence de toutes les séries adoptées en M4.
### B. Entité « paquet » (`content_bundle` : Édition, plage, `closed`) à côté du contenu (proposition GAP-6-01)
- Avantages : modélise bien les EPUB par plage.
- Inconvénients : deuxième chemin de rattachement à côté de la Variante ; ne couvre ni les tomes adoptés, ni les intégrales multi-Œuvres, ni la clé du manifeste.
### C. `export_item` (fichier ou objet exporté) + `unit_coverage` (support → Unités) dès le schéma M1a, manifeste clé par `(shelf_destination_id, export_item_id)`, invariant de chemin figé
- Avantages : une seule notion pour tomes adoptés, plages lncrawl, `rolling_tail`, assemblage M4, intégrales et recueils ; la présence d'une Unité se calcule par couverture ; le manifeste convient à tous les transports.
- Inconvénients : une table et une jointure de plus ; en M1a, la plage d'un tome n'est connue que par la source (« Vol.N ») ou une saisie manuelle.

## Décision
Option C (arbitrages F1, F3, F7).
- **`export_item`** : un fichier ou objet exporté par liaison d'étagère. **`unit_coverage`** : `(carrier_type ∈ {content, export_item, volume}, carrier_id, unit_id, extent ∈ {full, partial}, origin ∈ {comicinfo, filename, source_name, provider, manual}, confidence)` (EF-BIB-044). Elle remplace `covers[]` et `content_coverage`, et couvre aussi un fichier qui contient plusieurs Œuvres (réservé).
- **Invariant I20** : une Unité logique est présente si et seulement si une Variante exportée ou un support qui la couvre est présent ; suivi, `backfill --gaps` et I3 raisonnent sur la présence.
- **Filigrane** exprimé en numéros de chapitre ; une série « tomes seulement », ou dont la plage d'un tome est inconnue, passe en `waiting_state = coverage_unknown`, sortie `shelfhold adopt set-follow-from <série> <chapitre>`, jamais en suivi automatique (EF-EXP-053). En M1a, la plage se déduit du préfixe « Vol.N » de la source ou d'une saisie ; ComicInfo et fournisseurs en M2.
- **Contrat Source** : capacité « support couvrant une plage » (`ranged_content`) et drapeau « tome clos » (EF-SRC-032).
- **Manifeste** clé par `(shelf_destination_id, export_item_id)` avec un `locator` typé selon le transport (EF-EXP-058, ADR-0011 amendée) ; pour Kavita (`tree`), le chemin reste la clé de résolution.
- **Chemin figé (I22)** : le chemin d'un `export_item` ne dépend que des attributs figés à sa première exportation (série exportée figée, track, `number_norm`, part, `template_id@version`). Un tome tardif, un special ou un titre corrigé passent par ComicInfo et l'API Kavita ; un renommage n'est qu'une proposition appliquée sur action explicite. `Vol.X` n'entre pas dans le nom d'un chapitre de série en cours (tome dans ComicInfo seulement, vérifié par un golden `plugin/parse`) (EF-EXP-061, Q41).
- **Formats multiples** : une Variante porte 0..N contenus typés (`media_type`) ; la destination déclare `accepted_content_types` (GAP-6-10).
- **Aucun identifiant Shelfhold dans les fichiers exportés** (GAP-6-16, Q40) : ni `<Notes>` ComicInfo, ni `shelfhold:id` OPF. Raisons : discrétion (ADR-0030, un fichier peut quitter le foyer), le manifeste sauvegardé et le hash suffisent à reconnaître un fichier, `reconcile --adopt-after-restore` apparie par hash puis par chemin. Revenir sur ce choix imposerait de réécrire les fichiers (positions KOReader perdues) : il est pris maintenant.
- En M1a, pour les fichiers **écrits** par Shelfhold, la relation `export_item` → Unité est 1-1 et vérifiée par un invariant ; les supports adoptés ou importés peuvent couvrir N Unités.

## Conséquences
- Positives : plus de doublons tome/chapitres ; une seule relation réutilisée par l'assemblage M4, le transfert de progression entre Éditions (EF-BIB-026) et les intégrales ; manifeste prêt pour les destinations non arborescentes ; noms stables.
- Négatives : une série adoptée en tomes seuls demande une action de l'admin (`coverage_unknown`) ; une attribution tardive de tome n'apparaît pas dans le nom du fichier.

## Risque principal
Une plage de tome mal déduite (« Vol.N » absent ou faux) qui masque des chapitres réellement manquants. Mitigation : `confidence` et `origin` enregistrés, plage inconnue → `coverage_unknown` plutôt qu'une supposition ; tests `test_adopted_volume_only_series_never_auto_follows` et propriété « aucun cycle ne demande une Unité couverte ».

## Réversibilité
Moyenne : le schéma N-N est un sur-ensemble du 1-1, donc on peut toujours restreindre ; la décision « aucun identifiant dans les fichiers » est réversible pour les nouveaux fichiers seulement.

## Amendement passe 7 (2026-11-03)
G2, G9 (GAP-7-02, V1).
- `export_series` a son **propre ULID** ; la liaison `edition_export_series` relie les Éditions à la série distante ; l'ancienne clé de la passe 6 n'est plus que la règle de proposition ; fusion, séparation et rattachement de Série ne changent que la liaison, par plan explicite (EF-EXP-062, invariant I26, ADR-0043).
- `unit_coverage` porte dès M1a `start_locator?` et `end_locator?` (bornes de l'Unité dans le support), nulles si inconnues ; `document_alias` pointe vers `export_item_id` (ADR-0045, EF-LEC-014).
- I20 se décline en `present_for_acquisition(unit)` et `present_in(unit, shelf_destination)`.

## Amendement passe 8 (2026-11-03)
H1, H3 (GAP-8-01, GAP-8-11).
- **Aucune identité Shelfhold dans les fichiers exportés : décision maintenue.** Le chasseur de la passe 8 proposait une entrée annexe `shelfhold.json` dans chaque archive (CBZ, EPUB hors OPF) pour que la lignée survive à une perte de `/data`. Proposition **rejetée** : elle laisserait une trace de l'outil et des références de source dans chaque fichier, y compris ceux qui quittent le foyer (ADR-0030) ; les fichiers sont figés (I22) et l'annexe ne pourrait jamais être corrigée ni retirée sans réécriture ; elle changerait l'empreinte du fichier rendu (`file_hash`) à chaque évolution du format.
- **À la place : miroir de provenance par racine physique** (`<racine>/.shelfhold/manifest/`, ADR-0046, EF-EXP-063) : même information (export_item, couverture, Variante, `remote_unit_id`, Équipe, gabarit), à côté des fichiers, clé par chemin relatif et `file_hash`, relue par la réadoption, qui retrouve l'état `exported`. Le manque de lignée relevé par GAP-8-01 est ainsi corrigé, sous une autre forme que celle proposée (journal : PARTIELLE).
- ComicInfo `Translator` reçoit le libellé d'Équipe (métadonnée publique, utile au lecteur) ; `Web` reste vide par défaut.
- `remote_library_id` est une clé locale (ULID) de `remote_library` ; l'identifiant du serveur n'est qu'un cache (GAP-8-11, EF-EXP-062).

## Exigences liées
EF-BIB-044, EF-SRC-032, EF-EXP-058, EF-EXP-061, EF-EXP-053, EF-EXP-014, EF-EXP-039, EF-EXP-046, EF-BIB-040, EF-BIB-034, EF-EXP-045, ENF-QUAL-006, EF-EXP-062, EF-LEC-014, EF-EXP-063.

## Sources
- `deliverables/review/chasseur-trous-passe6.md` (GAP-6-01, GAP-6-03, GAP-6-07, GAP-6-10, GAP-6-16, GAP-6-19)
- `deliverables/review/explorateur-passe6.md` (U1)
- `deliverables/review/ARBITRAGES-passe6.md` (F1, F3, F7)
- https://wiki.kavitareader.com/guides/scanner/managefiles/
- https://github.com/mihonapp/mihon/blob/main/source-api/src/main/kotlin/eu/kanade/tachiyomi/source/model/SChapter.kt
- https://docs.anilist.co/reference/enum/mediarelation
- https://repository.ifla.org/handle/20.500.14598/40 (IFLA LRM)
- https://readium.org/webpub-manifest/
- `deliverables/review/chasseur-trous-passe7.md`, `deliverables/review/explorateur-passe7.md`, `deliverables/review/ARBITRAGES-passe7.md` (passe 7)
- `deliverables/review/chasseur-trous-passe8.md`, `deliverables/review/explorateur-passe8.md`, `deliverables/review/ARBITRAGES-passe8.md` (passe 8)

## Amendement passe 13 (2026-11-03)
N6 (AB4).

- **Règle anti-écho (avant M2)** : un fichier reçu d'une source dont le `file_hash` ou le `content_hash` correspond à un export existant (`export_record.file_hash`, anciens hashs de `document_alias`, `content`) est rattaché comme `replica` de l'`export_item` existant, jamais comme nouvelle Variante (I50). Cas typique : une source OPDS branchée sur le Kavita ou le Komga où Shelfhold exporte ; lier une telle source affiche « utilisez `readback` ». Les fichiers exportés ne portant aucune identité (I33), l'empreinte est la seule reconnaissance possible ; aucune donnée à migrer.
- Test : `test_opds_source_reimport_of_own_export_is_replica_not_variant`.

Source : `deliverables/review/chasseur-trous-passe13.md`, `deliverables/review/explorateur-passe13.md`, `deliverables/review/ARBITRAGES-passe13.md` (passe 13)
