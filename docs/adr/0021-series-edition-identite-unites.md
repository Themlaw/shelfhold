---
status: accepté (amende ADR-0009)
date: 2026-11-02
amended: 2026-11-02 (passe 2) ; 2026-11-03 (passe 6) ; 2026-11-03 (passe 7) ; 2026-11-03 (passe 8) ; 2026-11-03 (passe 10) ; 2026-11-04 (porteur 2)
decision-makers: mainteneur
---
# ADR-0021 — Entité Series, définition de l'Édition et identité des Unités

> **Amendée le 2026-11-02 (passe 2).** En cas de divergence, la section « Amendement passe 2 » prévaut sur le texte qui précède.

## Contexte et problème
ADR-0009 pose le modèle Work → Edition → Volume → Unit → Content. La revue d'évolutivité y a relevé quatre défauts.
- **Édition mal définie (OBJ-EVO-01).** L'Édition est dédoublonnée « par langue + groupe ». Mais l'équipe de scanlation change d'un chapitre à l'autre, et une édition officielle n'a pas d'équipe.
- **Clé d'Unité instable (OBJ-EVO-02, EVO-16).** L'Unité est identifiée par (édition, numéro). Or deux sources numérotent différemment, il existe des `10.5`, des `annual #1` qui se chevauchent avec `#1`, et des chapitres renumérotés.
- **Rien au-dessus de l'Œuvre (OBJ-EVO-10).** Une BD franco-belge par albums, une série de romans d'un même univers ou une intégrale regroupant plusieurs tomes ne s'expriment pas.
- **Identifiants externes limités à l'Œuvre (OBJ-EVO-03).** Les ISBN appartiennent pourtant aux Éditions ou aux Volumes.

## Facteurs de décision
- Une identité d'Unité qui survit aux renumérotations et aux doublons de numéro.
- Un regroupement optionnel au-dessus de l'Œuvre, sans rendre le cas simple (une Œuvre = une série Kavita) plus compliqué.
- Des identifiants externes au bon niveau (ISBN → Édition/Volume ; AniList → Œuvre).
- « Concevoir large, livrer étroit » : schéma présent dès M1, UI minimale.

## Options considérées
### A. Series optionnelle + Édition = (œuvre, langue, type de traduction, média, éditeur/officiel) + Unité identifiée par (source_binding, remote_unit_id) + identifiants polymorphes
- Avantages : chaque problème a une réponse locale. La Series est facultative, donc le cas manga ne change pas. Les numéros ne servent plus jamais de clé.
- Inconvénients : une table et une indirection de plus ; règle d'export Kavita à préciser (Series ou Work).
### B. Garder ADR-0009 et traiter les cas au fil de l'eau
- Avantages : rien à faire maintenant.
- Inconvénients : migration lourde plus tard (clé d'Unité), doublons silencieux et fusions erronées dès M1.
### C. Tags/collections à la place d'une entité Series
- Avantages : pas d'entité nouvelle.
- Inconvénients : pas d'ordre de lecture, pas d'identifiants externes de série, pas de règle d'export.

## Décision
Option A.
- **Series** (EF-BIB-025) : entité optionnelle au-dessus de Work, avec ordre de lecture. À l'export Kavita, la série Kavita est la Series si elle existe, sinon l'Œuvre. `content_coverage` (intégrales couvrant N tomes) est réservé à V2.
- **Édition** : définie par (œuvre, langue, type de traduction officielle/fan/originale, média texte/image/audio, éditeur ou « officiel »). L'**Équipe** de traduction est portée par la Variante d'Unité (`unit_variant`), pas par l'Édition.
- **Unité** (EF-BIB-002) : identité = `(source_binding, remote_unit_id)`. Les champs sont `source_order`, `number` (texte brut + clé de tri décimale), `part` et `track` (main, annual, special, extra, oneshot). Le tri total est `(track, number_sort, part, source_order, published_at, remote_unit_id)`. Le couple (édition, numéro) n'est **jamais** une clé.
- **Identifiants externes** (EF-BIB-003) : table polymorphe `external_id(entity_type, entity_id, provider, value)` sur Series, Work, Edition et Volume, avec unicité par `entity_type`. Les contributeurs utilisent le même schéma polymorphe. Un conflit produit une suggestion, jamais une fusion.
- **Fusions** : réversibles via `work_redirect` + `merge_log` (EF-BIB-019).
- **`work.kind`** : vocabulaire ouvert (table de référence), sans `audio`. Le média est porté par l'Édition.

## Conséquences
- Positives : migrations de source, imports `.tachibk`, trackers et kosync s'appuient sur une identité stable. Les BD par albums et les intégrales deviennent exprimables. Le dédoublonnage est plus sûr.
- Négatives : plus de tables. Il faut des tests de propriétés sur le tri des numéros et sur la règle d'export Series/Work, et une UI « Séparer » (M3).

## Risque principal
Mauvais rattachement automatique d'une Œuvre à une Series ou d'une Édition à une Œuvre. Mitigations : rattachement automatique **affiché** et annulable, journal des fusions de 30 jours, jamais de fusion sur le titre seul.

## Réversibilité
Moyenne : les tables sont additives. Retirer la Series plus tard est simple, mais revenir à une clé (édition, numéro) ne l'est pas.

## Amendement passe 2 (2026-11-02)
- **Préférence d'équipe minimale en M1** (B8, EF-BIB-028) : liste de priorité par Œuvre, défaut = première équipe vue ; une seule variante exportée par (numéro, part). L'UI de choix reste en M3 (EF-BIB-005).
- **Relier à une œuvre existante** (B9, EF-BIB-029) : une nouvelle liaison de source (après migration de source dans Suwayomi) se rattache à l'Œuvre existante ; seuls les numéros manquants sont récupérés ; l'identité d'Unité reste `(liaison, remote_unit_id)`, la correspondance entre liaisons se fait par `(track, number_sort, part)`.
- **Appariement adoption ↔ suivi** (B7, EF-EXP-038) : un fichier adopté est associé au `remote_unit_id` de la liaison confirmée.
- **Droits** : champ `rights` sur l'Édition (EF-BIB-033).

## Amendement passe 3 (2026-11-02)
- **Identité à deux niveaux** (C1, EF-BIB-034) : l'identité passe 2 « Unité = (liaison, `remote_unit_id`) » empêchait de regrouper les équipes (chaque équipe publie un chapitre distant différent). Désormais : **Unité logique** `UNIQUE(edition_id, track, number_norm, part)` ; **Variante** `UNIQUE(source_binding_id, remote_unit_id)` rattachée à une Unité logique. Replis : sans numéro → track « non numéroté », place = `source_order` (rattachement par titre normalisé seulement en suggestion) ; égalité → la Variante la plus ancienne garde la place ; re-publication par la même équipe → nouvelle révision (garde-fou EF-BIB-024). Invariants I1–I15 listés dans `02-architecture.md` §3.1 bis et testés par propriétés (ENF-QUAL-006). La correspondance entre liaisons pour `relink` reste `(track, number_sort, part)`.
- **Équipe propre à la série** (C16, EF-BIB-035) : `team(source_binding_id, label)` ; jamais d'Équipe globale (un agrégateur attribue des libellés génériques par série). Règle par défaut d'EF-BIB-028 : équipe majoritaire déjà présente, puis la plus à jour, puis la plus complète, hors équipes inactives > N mois, repli journalisé (remplace « première vue »).
- **Âge par Édition, âge effectif = maximum** (C8, EF-BIB-037) ; **Œuvres liées** (`work_relation`, « adaptation de », C21, EF-BIB-039) ; **politique de tomes** des romans en cours (`edition.volume_policy`, C20, EF-BIB-040) ; clé d'unicité de l'Édition sans NULL (GAP-3-25).
- Réversibilité revue : cette correction se fait **avant M1** ; après remplissage du manifeste, elle exigerait une migration de données.

## Amendement passe 6 (2026-11-03)
F1, F2, U6 (GAP-6-01, GAP-6-02, GAP-6-19).
- **Clé d'Édition** : *(œuvre, langue, type de traduction, média, éditeur/officiel, `text_version`)*. `text_version` vaut `''` par défaut (rien ne change pour le manga) ; pour la prose (`kind` roman, livre, light novel), un traducteur différent ou une version abrégée, révisée ou jeunesse donne une Édition distincte ; pour le manga et le webtoon, une Équipe reste une Variante (EF-BIB-001). Crochet réservé `translated_from_edition_id?` pour les traductions relais (U6).
- La langue de la clé est une étiquette **BCP 47 canonique** ; `all`, `multi` et `und` n'y entrent jamais (ADR-0042, invariant I23).
- **Couverture** : un support (fichier adopté, contenu reçu, tome, `export_item`) peut couvrir plusieurs Unités logiques ; la relation est `unit_coverage` (ADR-0040, EF-BIB-044) et la présence d'une Unité se calcule par couverture (invariant I20). Le filigrane de suivi est exprimé en numéros de chapitre ; une série adoptée en tomes seuls passe en `waiting_state = coverage_unknown`, jamais en suivi automatique (EF-EXP-053).
- Le vocabulaire `work_relation.kind` reste ouvert (comme `work_kind`), pour accueillir les relations AniList (`COMPILATION`, `CONTAINS`, `SIDE_STORY`…).

## Amendement passe 7 (2026-11-03)
G3, G6 (GAP-7-03, GAP-7-06).
- **Équipe rattachée à l'Édition** : identité `(edition_id, libellé normalisé)`, provenance gardée par `team_alias(source_binding_id, label)` ; aucune fusion entre séries ; `relink`, migration de source et `migrate_ref` conservent l'Équipe et la préférence (EF-BIB-035, invariant I27). La définition de la passe 3 (« équipe propre à la liaison de source ») est remplacée.
- **Discriminant de traduction selon la provenance** (EF-BIB-049) : le contrat Source déclare `translation_nature` ; une traduction publiée ou de catalogue donne une Édition (`text_version`), une traduction communautaire d'un service connecté reste une Équipe, quel que soit le `kind` (un roman web traduit par des équipes qui se relaient reste une seule Édition). La règle par `kind` de la passe 6 ne vaut plus que pour `unknown`.
- `text_version` est dérivé d'une **référence** (`text_version_contributor_id?` ou libellé contrôlé), jamais d'une chaîne de nom ; une fusion de contributeurs qui ferait coïncider deux Éditions passe par un plan de fusion d'Éditions (ADR-0043).

## Amendement passe 8 (2026-11-03)
H2, H3 (GAP-8-02, GAP-8-03, GAP-8-04).
- **Révision ou Variante** : un `remote_unit_id` distinct crée **toujours** une nouvelle Variante ; une révision ne concerne que le même `(source_binding_id, remote_unit_id)` et s'ajoute à `variant_revision` (ajout seul) ; `remote_unit_id` n'est jamais réécrit (I32).
- **Collision dans une même liaison** : une nouvelle Variante qui tombe sur une place tenue par une Variante vivante de la même liaison ouvre `numbering_conflict` ; si l'ancienne est `removed`, la nouvelle porte `supersedes_variant_id` et la disparition ne compte pas comme erreur de contenu.
- **Segment de numérotation** : la place devient `(edition_id, track, number_segment, number_norm, part)`, `number_segment = ''` par défaut ; le filigrane devient `(number_segment, number_norm)` (I34) ; la forme du nom d'un segment non vide est tranchée par un golden `plugin/parse` en M1a (Q50), sans marqueur de saison que Kavita lirait comme un tome.
- Backlog : `packaging` dans la clé d'Édition en M2 (omnibus, kanzenban) ; `carrier_type = unit` pour un chapitre re-découpé (M2).

## Amendement passe 10 (2026-11-03)
K3, K6 (GAP-10-03, Y2).
- **Clé d'Édition** : colonne `performance` à sentinelle `''`, dans la clé dès le schéma de M1a (même mécanisme que `packaging`), pour séparer deux lectures audio d'un même texte (narrateur, version dramatisée) ; dérivée du rôle `narrator` quand l'audio arrive ; en attendant, `audio/*` est refusé au dossier d'import (EF-SRC-012).
- **Identité des Unités d'un service connecté** : `remote_unit_id` est l'identité de la source amont (Suwayomi : `sourceId` + `chapter.url`), jamais l'identifiant de la base de l'outil ; voir ADR-0043.

## Amendement porteur 2 (2026-11-04)
- **Suivi porté par l'Édition** (`review/ARBITRAGES-porteur2.md`, Q3a ; EF-SRC-037, EF-EXP-053) : `series_follow` est clé par `edition_id` (et non plus par `source_binding_id`) dès le schéma de M1a ; les liaisons d'une Édition sont **classées** (`source_binding.priority`, unique parmi les liaisons actives) ; la sélection d'une Unité nouvelle suit l'ordre pierres tombales → filtres → préférence d'équipe → meilleur rang, et n'émet qu'une demande par cycle (I52). Le modèle Unité logique / Variante (I1) porte déjà plusieurs Variantes d'une même place venues de liaisons différentes : aucune migration n'est nécessaire, seul le grain du suivi change avant le premier code. Trous de numérotation : EF-BIB-056 (places entières seulement, jamais les numéros décimaux ni les `part`).

## Exigences liées
EF-BIB-001, EF-BIB-002, EF-BIB-003, EF-BIB-004, EF-BIB-005, EF-BIB-019, EF-BIB-025, EF-EXP-032, EF-SRC-022, EF-INT-008, EF-BIB-028, EF-BIB-034, EF-BIB-035, EF-BIB-037, EF-BIB-039, EF-BIB-040, ENF-QUAL-006, EF-BIB-044, EF-SRC-032, ENF-I18N-005, EF-EXP-053, EF-BIB-049, EF-BIB-029, EF-BIB-050, EF-SRC-036, EF-SRC-012.

## Sources
- https://wiki.kavitareader.com/guides/scanner/ (une série Kavita = un dossier/nom de série)
- https://github.com/anansi-project/comicinfo (`Series`, `Number`, `Volume`, `SeriesGroup`, `AlternateSeries`)
- https://manual.calibre-ebook.com/metadata.html (identifiants multiples par livre)
- https://github.com/mihonapp/mihon (modèle série + chapitre `float`, limites constatées)
- `deliverables/review/contradicteur-evolutivite.md` (OBJ-EVO-01, -02, -03, -10, -16, -42), ARBITRAGES A4–A6
- `deliverables/review/chasseur-trous-passe3.md` (GAP-3-01, GAP-3-08), `deliverables/review/explorateur-passe3.md` (P3, P7, P12)
- `deliverables/review/chasseur-trous-passe7.md`, `deliverables/review/explorateur-passe7.md`, `deliverables/review/ARBITRAGES-passe7.md` (passe 7)
- `deliverables/review/chasseur-trous-passe8.md`, `deliverables/review/explorateur-passe8.md`, `deliverables/review/ARBITRAGES-passe8.md` (passe 8)
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
