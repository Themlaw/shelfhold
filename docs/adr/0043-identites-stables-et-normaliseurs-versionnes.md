---
status: accepté
date: 2026-11-03
amended: 2026-11-03 (passe 8) ; 2026-11-03 (passe 10) ; 2026-11-03 (passe 11)
decision-makers: mainteneur
---
# ADR-0043 — Identités stables et normaliseurs versionnés

## Contexte et problème
La passe 7 a suivi chaque identité du modèle à travers le cycle de vie (fusion, séparation, `relink`, migration de source, mise à jour de Shelfhold, mise à l'abri, fan-out). Cinq identités posées en M1a ne résistaient pas à au moins un de ces événements :
- **Octets** (GAP-7-01) : `replica` n'avait qu'un `hash`, sans dire s'il mesurait le contenu reçu ou le fichier rendu pour Kavita (ComicInfo injecté, OPF réécrit). Une mise à l'abri depuis `/library` laissait dans `/store` un fichier qui ne correspondait plus à `original_sha256`, et un fan-out pouvait propager le ComicInfo d'une destination vers une autre ; `independent_copies()` comparait des grandeurs différentes.
- **Séries exportées** (GAP-7-02) : la clé d'`export_series` était **calculée** à partir de l'Œuvre ou de la Série, que la fusion et la séparation réécrivent, alors que le chemin exporté est figé (I22).
- **Équipes** (GAP-7-03) : l'identité `(source_binding_id, libellé)` changeait à chaque `relink` ou migration de source ; la règle « équipe majoritaire » ne retrouvait plus l'équipe et changeait de traduction en cours de série.
- **Traductions** (GAP-7-06) : la règle « deux traducteurs = deux Éditions » selon le `kind` coupait en morceaux les romans web traduits par des équipes de fans qui se relaient ; `text_version` portait une chaîne de nom alors que les traducteurs sont des `contributor` fusionnables.
- **Normaliseurs** (GAP-7-08) : `number_norm`, `normalize_lang()`, `name_normalizer` et la normalisation du traducteur produisent des clés uniques ; leur comportement changera avec les versions, et rien ne disait quelles lignes recalculer.

## Facteurs de décision
- Une identité posée dans le schéma de M1a doit survivre à tous les événements du cycle de vie connus, sans migration de sens après coup.
- Aucune fusion ni séparation silencieuse ; toute réaffectation est un plan visible.
- Coût faible aujourd'hui : colonnes et tables d'association, presque sans code.
- Ne rien recalculer à la lecture.

## Options considérées
### A. Garder les clés calculées et réparer au cas par cas
- Avantages : aucun travail en M1a.
- Inconvénients : chaque événement du cycle de vie demande une migration de données réelles ; doublons et changements de traduction silencieux entre-temps.
### B. Identités propres, provenance gardée à côté, normaliseurs versionnés
- Avantages : chaque identité est stable par construction ; les événements ne touchent que des liaisons ; une mise à jour de normaliseur est une migration ciblée et prévisualisée.
- Inconvénients : quelques tables et colonnes de plus ; une fonction `content_hash()` à maintenir par format.
### C. Tout ramener à un identifiant externe (ISBN, AniList, Wikidata)
- Avantages : identifiants partagés.
- Inconvénients : couverture faible sur la longue traîne (ADR-0042) ; ne règle ni les octets ni les équipes.

## Décision
Option B (arbitrages G1, G2, G3, G6, G8).
- **Octets (EF-BIB-048, invariant I25)** : `content_hash` = charge utile d'origine normalisée (images dans l'ordre sans fichiers annexes ; EPUB sans l'OPF réécrit ni les fichiers ajoutés) ; `file_hash` = fichier rendu par `(shelf_destination, export_item)`. `independent_copies()` compte sur `content_hash`. `/store` garde toujours l'original reçu ; à défaut de source, l'original est extrait du fichier rendu et vérifié. Le rendu est refait par destination à partir du canonique.
- **Séries exportées (EF-EXP-062, I26)** : ULID propre, liaison `edition_export_series` ; l'ancienne clé ne sert qu'à proposer à la création ; fusion, séparation et rattachement de Série ne changent que la liaison, par plan explicite.
- **Équipes (EF-BIB-035, I27)** : identité `(edition_id, libellé normalisé)` + `team_alias(source_binding_id, label)` ; `relink` transporte les alias ; préférence attachée à l'Édition.
- **Traductions (EF-BIB-049)** : discriminant selon `translation_nature` déclarée par la source (publiée → `text_version`, communautaire → Équipe) ; `text_version` dérivé d'une référence (contributeur ou libellé contrôlé) ; fusion de contributeurs → plan de fusion d'Éditions.
- **Normaliseurs (ENF-OPS-021, I29)** : `normalizer_id@version` et valeur brute stockés avec chaque clé dérivée ; changement = nouvelle version + migration explicite avec aperçu des fusions et doublons (`shelfhold normalize migrate`), jamais de recalcul à la lecture.

## Conséquences
- Positives : plus de migration de sens prévisible sur ces cinq identités ; fixité vérifiable de `/store` (BagIt M4) ; plus de changement de traduction en cours de série après un `relink` ; romans web en une seule Édition.
- Négatives : schéma plus large ; une extraction d'original peut échouer (Unité laissée « irremplaçable sans copie », jamais une copie fausse) ; les sources doivent déclarer `translation_nature` (défaut `unknown`).

## Risque principal
Une fonction `content_hash()` qui diffère selon la version et rend « non indépendantes » des copies identiques. Mitigation : `content_hash()` est elle-même un normaliseur versionné, avec vecteurs de test par format ; un changement passe par une migration explicite.

## Réversibilité
Haute avant le premier déploiement, moyenne ensuite : les tables de liaison et d'alias sont additives et les valeurs brutes permettent de recalculer.

## Amendement passe 8 (2026-11-03)
H2, H4 (GAP-8-02, W3).
- Identité des Variantes (ADR-0021 amendée) : `remote_unit_id` n'est jamais réécrit ; révisions en ajout seul ; `supersedes_variant_id`.
- `number_segment` est une clé d'identité : il porte `number_segment_norm_by` (`normalizer_id@version`) et sa valeur brute, et suit la règle de migration explicite (ENF-OPS-021, I29).
- `external_id` reçoit `id_scheme?` et un état `current`/`superseded` : une renumérotation par un fournisseur ajoute la nouvelle valeur sans écraser l'ancienne (EF-BIB-003, M2).
- `remote_library` : la bibliothèque distante a une identité locale stable, l'identifiant du serveur n'est qu'un cache (EF-EXP-062).

## Amendement passe 9 (2026-11-03)
J1, J5 (X1, GAP-9-01, GAP-9-11, GAP-9-12).
- **Confiance du contenu** : `content_hash` reste l'empreinte de l'**original** ; le dérivé neutralisé d'un EPUB `untrusted` reçoit `derived_hash` et `derived_by = xhtml-neutralizer@vN` (fonction versionnée, comme un normaliseur : changer de version produit un nouveau dérivé sans toucher `content_hash`) ; `file_hash` est calculé sur le rendu du dérivé (ADR-0047).
- `number_segment_ordinal` est produit par le même normaliseur que `number_segment` ; toutes les comparaisons de filigrane et de plage utilisent l'ordinal.
- Les références distantes sont des identités : elles ne contiennent jamais de secret et ne changent pas à la rotation d'une clé (EF-SRC-035).
- `remote_library` est clé par `(export_target_id, remote_ref)` (bibliothèque du serveur), avec une table de liaison vers les étagères.

## Amendement passe 10 (2026-11-03)
K3 (GAP-10-03).
- `remote_unit_id` = **identité de la source amont** : pour Suwayomi, `suwayomi:<sourceId>` + `chapter.url` normalisé (comme Mihon), jamais l'entier local de Suwayomi, qui renumérote ses chapitres à la réinstallation ou à la restauration d'une sauvegarde. L'entier devient `unit_variant.fetch_handle`, cache ré-résolu comme les identifiants Kavita.
- Conséquences évitées : Variantes en double et `numbering_conflict` en masse après une réinstallation, pierres tombales contournées, collisions entre deux instances Suwayomi, correspondance impossible pour `migrate_ref` (M3). Invariant I42.

## Amendement passe 11 (2026-11-03)
L1 (GAP-11-01). Règle de registre : une version de normaliseur (`normalize_lang()`, `remote_unit_id`, `number_norm`, clé d'Édition) ou d'algorithme d'empreinte (`content_hash`, `derived_hash`) **citée par au moins une pierre tombale n'est jamais retirée du code** ; un contrôle CI (`test_cited_normalizer_version_never_removed`) échoue sinon. Les pierres tombales gardent les **valeurs brutes** chiffrées et les `normalizer_id@version` en vigueur à la purge (ADR-0048) ; le test d'appartenance re-normalise la pierre et le candidat sous le normaliseur courant et chaque version citée. Les migrations d'identité — `normalize migrate` (M1a), fusion d'Éditions (EF-BIB-049, M2), `migrate_ref` et alias (EF-SRC-022, M3), `replaced_by` (EF-SRC-030, M6) — **consultent les pierres tombales** : une ligne vivante qui, migrée, correspondrait à une pierre est bloquée (`tombstone.migration_hit`) et l'alias inverse est passé au test (`test_identity_migration_consults_tombstones`). La conservation des valeurs brutes (`number_raw`, `lang_raw`, URL brute) déjà exigée par cette ADR rend ce test possible côté candidat. Invariant I46.

## Exigences liées
EF-BIB-048, EF-EXP-062, EF-BIB-035, EF-BIB-049, ENF-OPS-021, EF-BIB-045, EF-EXP-032, EF-BIB-028, EF-BIB-029, ENF-QUAL-006, EF-BIB-050, EF-BIB-003, EF-BIB-051, EF-SRC-035, EF-SRC-036, EF-SRC-008, EF-BIB-054, EF-SRC-022, EF-SRC-030.

## Sources
- `deliverables/review/chasseur-trous-passe7.md` (GAP-7-01, GAP-7-02, GAP-7-03, GAP-7-06, GAP-7-08, GAP-7-15)
- `deliverables/review/ARBITRAGES-passe7.md` (G1, G2, G3, G6, G8)
- https://github.com/anansi-project/comicinfo
- https://www.w3.org/TR/epub-33/
- https://www.rfc-editor.org/rfc/rfc8493 (BagIt)
- https://github.com/unicode-org/cldr
- `deliverables/review/chasseur-trous-passe8.md`, `deliverables/review/explorateur-passe8.md`, `deliverables/review/ARBITRAGES-passe8.md` (passe 8)
- `deliverables/review/chasseur-trous-passe9.md`, `deliverables/review/explorateur-passe9.md`, `deliverables/review/ARBITRAGES-passe9.md` (passe 9)
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)
