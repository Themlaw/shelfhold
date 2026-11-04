---
status: accepté, amendée par ADR-0021
date: 2026-11-02
amended: 2026-11-02 (passe 1) ; 2026-11-03 (passe 6)
decision-makers: mainteneur
---
# ADR-0009 — Modèle unifié et identité stable

> **Amendée le 2026-11-02 (passe 1).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Les contenus arrivent sous forme d'images (manga, webtoon), de texte (romans web) ou de fichiers (EPUB, PDF), depuis des sources hétérogènes, souvent en plusieurs langues et scanlations. Mihon n'a aucune identité globale ; sa migration de source est une recherche par titre confirmée à la main. L'import `.tachibk`, la migration, les trackers, kosync et la synchro Kobo dépendent tous d'identités stables (07-explore, top 10 n°1). Ajouter cela après coup coûterait une migration de toute la bibliothèque.

## Facteurs de décision
- Couvrir images, texte, fichier et audio (crochet D43) dans un seul schéma.
- Dédoublonnage fiable sans fusion silencieuse.
- Multi-sources, multi-scanlators, numérotation exotique (`10.5`, `annual`, `#0`).
- Progression robuste au changement de rendition.

## Options considérées
### A. Work → Edition → Volume → Unit → Content, `external_ids`, `SourceItem`, source bindings, variantes, numéro = clé de tri décimale + libellé
- Avantages : un modèle unique pour la migration, la préférence de scanlator, les doublons et la numérotation des comics (07 #7, #8, #21) ; identifiants externes façon Calibre (`identifiers` dict).
- Inconvénients : plus de tables et de jointures ; UI de dédoublonnage à concevoir.
### B. Modèle Mihon (série + chapitre `float`, `(source_id, url)`)
- Avantages : simple, compatible avec les sauvegardes Mihon.
- Inconvénients : pas de volume, `chapter_number` en float, pas de langue par chapitre, pas d'identité inter-sources.
### C. Modèle par format (tables séparées manga / livres)
- Avantages : chaque domaine reste simple.
- Inconvénients : recherche, export et lecture dupliqués ; une Œuvre multi-format (LN + manga + audio) devient impossible.

## Décision
Option A (D12, D13, D29, D30, D31, D44). Les points clés :
- `SourceItem = (plugin_id, source_key, remote_id)`.
- Table `external_ids(work_id, provider, value)`.
- Work ← N `source_bindings` (priorité, actif).
- `UnitVariant` (groupe, langue, source) avec politique de groupe préféré.
- Numéro stocké comme clé de tri `Decimal` en texte + libellé brut.
- `availability` par unité (`free`/`purchased`/`expiring`).
- Hash par rendition, dont le hash partiel KOReader.

Dédoublonnage :
- Œuvre : par `external_ids`, puis par titre normalisé, mais seulement comme **suggestion confirmée** par l'utilisateur.
- Édition : par langue + groupe.
- Unité : par (édition, numéro).

## Conséquences
- Positives : import `.tachibk`, migration, trackers et kosync sans refonte du schéma ; Western comics et magazines possibles plus tard.
- Négatives : complexité initiale ; tests de propriétés nécessaires sur le parse/tri des numéros.

## Risque principal
Fusions erronées d'Œuvres (deux éditions FR/EN ou un reboot). Mitigation : jamais de fusion automatique sur titre seul ; historique de provenance ; fusion réversible.

## Réversibilité
Faible : le modèle de données est le socle ; on peut l'étendre (champs additifs) mais pas le refondre sans migration lourde.

## Amendement passe 1 (2026-11-02)
Amendée par **ADR-0021** (A4, A5, A6), qui fait foi sur les points suivants :
- **Édition** = (Œuvre, langue, type de traduction, média, éditeur/officiel). L'**Équipe** (scanlation) n'est plus une clé d'Édition : elle est portée par la Variante d'Unité (`UnitVariant`).
- **Identité d'une Unité** = `(source_binding, remote_unit_id)`, avec `source_order`, `number` (texte brut + clé de tri décimale), `part`, `track` (main/annual/special/extra/oneshot). Le dédoublonnage « Unité par (édition, numéro) » ci-dessus est **abandonné comme clé** : il ne sert plus que de suggestion.
- **Series** optionnelle au-dessus de Work (EF-BIB-025) ; `external_id` et `contributor` **polymorphes** `(entity_type, entity_id)` sur Series/Work/Edition/Volume (EF-BIB-003) ; `content_coverage` réservé V2.
- `audio` retiré de `work.kind` (le média est porté par l'Édition).
- **Fusions** : rattachement automatique toujours affiché et annulable ; « Séparer » + `work_redirect` + `merge_log` annulable 30 jours (EF-BIB-019).
- **Alias de source** (`source_aliases`, EF-SRC-022) dès le schéma M1.
- **Hash KOReader** : calculé sur le **fichier exporté**, table d'alias à chaque réécriture (EF-LEC-009, ENF-QUAL-005).

## Amendement passe 6 (2026-11-03)
F8, F2 (GAP-6-08, GAP-6-02).
- **Identifiants** : toutes les entités exposées (API, fichiers sur disque, export intégral, webhooks M4, `document_alias` M5) ont un identifiant **ULID opaque**, jamais un entier séquentiel ; des entiers peuvent servir de clés internes de jointure, jamais à l'extérieur (ENF-OPS-020).
- **Autorité** : la base fait autorité ; `unit.json` et `work.json` sont des projections régénérables, rangées à plat par ULID, indépendantes de la hiérarchie Œuvre → Édition, donc insensibles aux fusions, séparations et `relink` (ADR-0039 amendée).
- La clé d'Édition reçoit le discriminant `text_version` (ADR-0021 amendée, EF-BIB-001) et ses langues sont canoniques (ADR-0042).

## Exigences liées
EF-BIB-001, EF-BIB-002, EF-BIB-003, EF-BIB-004, EF-BIB-005, EF-BIB-009, EF-BIB-014, EF-BIB-019, EF-BIB-025, EF-LEC-007, EF-LEC-009, EF-INT-008, EF-SRC-022, ENF-OPS-020, EF-BIB-044, ENF-I18N-005.

## Sources
- https://github.com/mihonapp/mihon/tree/main/app/src/main/java/eu/kanade/tachiyomi/data/track
- https://mihon.app/docs/guides/backups
- https://komga.org/docs/guides/koreader
- https://github.com/Snd-R/komf
- https://github.com/Suwayomi/Suwayomi-Server/issues/159
