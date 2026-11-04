---
status: accepté
date: 2026-11-03
amended: 2026-11-03 (passe 10)
decision-makers: mainteneur
---
# ADR-0046 — Miroir de provenance par racine physique

## Contexte et problème
Un fichier rangé dans `/library` est **figé** : son chemin ne dépend que d'attributs figés (I22) et il n'est jamais réécrit pour un simple changement de métadonnées, sous peine de perdre la position KOReader (EF-EXP-040). Par discrétion, il ne porte aucun identifiant Shelfhold (ADR-0040, ADR-0030, Q40). Sa lignée (Unités couvertes, Variante, clé de source et `remote_unit_id`, Équipe, gabarit) ne vit donc que dans la base de `/data`.

Le chasseur de la passe 8 (GAP-8-01) montre la conséquence : après une restauration en retard (EF-INT-026, parcours de M1a), une perte de `/data`, une réinstallation ou un déplacement de disques avec base perdue, `reconcile --adopt-after-restore` et l'adoption ne peuvent ranger ces fichiers qu'en `adopted`. La lignée est perdue : pas de réexport d'un manquant, pas d'upgrade, équipe inconnue qui fausse la règle de l'« équipe majoritaire », filigrane à reposer à la main. Et comme les fichiers sont figés, une décision prise plus tard ne s'appliquerait jamais aux fichiers déjà rangés.

## Facteurs de décision
- Lignée récupérable sans `/data`, pour tout fichier écrit dès M1a.
- Discrétion : rien dans les fichiers, rien qui quitte le foyer (ADR-0030).
- Fichiers figés : pas de réécriture, pas d'empreinte de fichier qui change avec un format.
- La base reste l'autorité ; ce qui est sur disque est une projection régénérable (ENF-OPS-020).

## Options considérées
### A. Entrée annexe `shelfhold.json` dans chaque archive (proposition GAP-8-01)
- Avantages : la lignée voyage avec chaque fichier, même déplacé à la main.
- Inconvénients : trace de l'outil et références de source dans chaque fichier, y compris ceux qui quittent le foyer ; annexe figée, jamais corrigible sans réécriture ; `file_hash` dépendant du format de l'annexe ; à retirer pour `egress = externe`, donc deux rendus.
### B. Rien de plus que le manifeste sauvegardé dans `/data` (situation passe 7)
- Avantages : aucun travail.
- Inconvénients : la lignée disparaît avec `/data` ou avec une sauvegarde en retard.
### C. Miroir du manifeste par racine physique, dans `<racine>/.shelfhold/manifest/`
- Avantages : la lignée vit à côté des fichiers, sur le même disque, et voyage avec eux (copie de disque, rsync) ; rien dans les fichiers ; format versionné et régénérable ; un seul rendu.
- Inconvénients : un fichier déplacé à la main **hors** de sa racine perd son entrée (retour à l'appariement par hash) ; un dossier caché que l'utilisateur peut effacer ; une écriture de plus par fichier.

## Décision
Option C (arbitrage H1).
- **Emplacement** : `<racine physique>/.shelfhold/manifest/` pour chaque racine de `PathMap` en mode `rw` (EF-EXP-059), à côté de `staging/` et `recycle/` ; exclu de Kavita (`ExcludePatterns`, test nightly « jamais indexé »).
- **Forme** : JSON Lines par liaison et par série exportée, `schema_version = "majeur.mineur"` (ADR-0039) ; une ligne par `export_item`, **clé `(chemin relatif, file_hash)`** ; contenu : `export_item` (ULID, `frozen_attrs`), Unités couvertes et bornes (`unit_coverage`), Variante (`source_key`, sous-source, `remote_unit_id`), `content_hash`, Équipe (ULID, libellé), `template_id@version`, Édition, série exportée, date d'écriture. Ni URL, ni domaine de source, ni secret.
- **Écriture** : par le cœur, sous intention (EF-EXP-041), dans la même session d'écriture que le fichier, après lui, par écriture atomique ; la base fait autorité et `shelfhold meta rebuild` régénère le miroir.
- **Lecture** : réconciliation, `reconcile --adopt-after-restore` (ADR-0044), adoption, réadoption après réinstallation, `--rebaseline-inodes` (M1b) : `file_hash` concordant → état `exported` avec la lignée ; sinon, appariement par hash et chemin, état `adopted`.
- **Jamais** : dans un fichier exporté ; vers une destination `egress = externe` ; dans un paquet de diagnostic (invariant I33).
- ComicInfo `Translator` = libellé d'Équipe ; `Web` vide par défaut.

## Conséquences
- Positives : une restauration, une perte de `/data` ou une réinstallation ne font plus perdre la lignée des fichiers déjà rangés ; la discrétion est intacte ; le miroir aide aussi au déplacement de disques (GAP-8-07).
- Négatives : un fichier écrit en plus par export (petit, ajout en fin de fichier JSON Lines, compacté dans la fenêtre de maintenance) ; un miroir effacé par l'utilisateur fait retomber sur l'appariement par hash.

## Risque principal
Miroir désynchronisé de la base (écriture interrompue, édition manuelle). Mitigation : écriture sous intention et après le fichier, clé `file_hash` (une ligne qui ne concorde pas est ignorée), régénération depuis la base, comparaison au démarrage comme pour `unit.json`.

## Réversibilité
Haute : le miroir est une projection ; on peut le supprimer ou en changer le format par migration en avant. Le choix « rien dans les fichiers » reste réversible pour les seuls nouveaux fichiers (ADR-0040).

## Amendement passe 9 (2026-11-03)
J5 (GAP-9-02, -03, -09, -10, -14, -15, -16). Format **1.1** (migration en avant sans perte, ADR-0039).
- Lignée **par entrée de couverture** : un support à plage ou un tome adopté couvre N Unités, éventuellement d'équipes différentes ; chaque entrée porte sa Variante, son `remote_unit_id` et son Équipe ; chaque empreinte porte sa fonction (`content_hash_by`, `derived_hash`/`derived_by`).
- Descripteur de liaison (référence de série distante, sous-source) et clé d'Édition dans chaque ligne ; une ligne non résolue reste `unresolved`, jamais effacée par `meta rebuild` ni par la réadoption.
- **Entrée non fiable** : taille bornée, schéma strict ; une ligne n'accorde que la lignée, si `file_hash` concorde et si ses ULID sont cohérents, jamais âge, étagère ni visibilité.
- Droits `0700`/`0600` ; aucun miroir sur une racine `adopt_ro` (lignée écrite sur la racine `rw` au rapatriement) ; chemin réservé, écrit par l'hôte seul (I37).

## Amendement passe 10 (2026-11-03)
K9 (GAP-10-13).
- Format **1.2** : le miroir porte aussi les **décisions de suivi** (`follow_suppressed`, `follow_from`) et l'**âge classé** ; après une perte de `/data` sans sauvegarde, « ne plus suivre » (restrictif) est réappliqué tel quel, l'âge revient en **suggestion** seulement (le miroir reste une entrée non fiable qui n'accorde ni âge ni étagère).

## Exigences liées
EF-EXP-063, EF-INT-026, EF-EXP-041, EF-EXP-059, EF-EXP-043, EF-EXP-058, ENF-OPS-020, EF-EXP-001, EF-EXP-051, EF-EXP-064.

## Sources
- `deliverables/review/chasseur-trous-passe8.md` (GAP-8-01), `deliverables/review/ARBITRAGES-passe8.md` (H1)
- https://wiki.kavitareader.com/guides/scanner/managefiles/ (dossiers et fichiers ignorés par le scanner)
- https://github.com/anansi-project/comicinfo (champs `Translator`, `Web`)
- https://www.rfc-editor.org/rfc/rfc8493 (BagIt : manifeste à côté de la charge utile)
- https://jsonlines.org/
- `deliverables/review/chasseur-trous-passe9.md`, `deliverables/review/explorateur-passe9.md`, `deliverables/review/ARBITRAGES-passe9.md` (passe 9)
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
