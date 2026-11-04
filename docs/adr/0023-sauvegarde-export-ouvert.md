---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 2) ; 2026-11-03 (passe 5) ; 2026-11-03 (passe 7) ; 2026-11-03 (passe 10) ; 2026-11-03 (passe 12)
decision-makers: mainteneur
---
# ADR-0023 — Sauvegarde, restauration et export intégral en format ouvert

> **Amendée le 2026-11-02 (passe 2).** En cas de divergence, la section « Amendement passe 2 » prévaut sur le texte qui précède.

## Contexte et problème
La version 1 prévoyait un `VACUUM INTO` nocturne, écrit dans `/data`, c'est-à-dire sur le même disque que la base. La revue OPS (OBJ-OPS-03) et le chasseur de trous (GAP-1-02, -03) ont relevé plusieurs problèmes.
- La clé maîtresse qui chiffre les secrets n'était pas sauvegardée : une sauvegarde restaurée était inutilisable.
- Les manifestes d'export n'étaient pas couverts.
- Aucune alerte n'était émise en cas d'échec, et aucune restauration n'était testée.
- La GC pouvait supprimer des fichiers encore référencés par une sauvegarde plus ancienne.
- Aucun export dans un format ouvert ne permettait de quitter le projet ou de survivre à sa mort (R4), contrairement à `document_exporter` de Paperless-ngx.

## Facteurs de décision
- Une restauration complète, y compris des secrets, sur une autre machine.
- Des objectifs explicites : RPO 24 h, RTO 1 h.
- Une sauvegarde hors du disque de données.
- Une portabilité des données indépendante du schéma interne.
- Une procédure testée en CI, pas seulement documentée.

## Options considérées
### A. Jeu de sauvegarde complet vers une destination distincte + export intégral ouvert séparé
- Avantages : couvre la panne de disque, la perte de la clé et la sortie du projet. Restauration testable.
- Inconvénients : deux mécanismes à maintenir (sauvegarde binaire et export ouvert). Une phrase de passe à faire conserver par l'utilisateur.
### B. Déléguer entièrement à la sauvegarde de l'hôte (restic, Borg, snapshots)
- Avantages : rien à coder.
- Inconvénients : une copie à chaud de SQLite est incohérente sans `VACUUM INTO`. La clé maîtresse est souvent hors du périmètre. Aucune alerte côté application. Pas de format ouvert.
### C. Seulement un export ouvert (JSON + fichiers)
- Avantages : un seul mécanisme, portable.
- Inconvénients : plus lent, et perte de l'état technique (file, journaux). Le RTO est plus long.

## Décision
Option A.

**Sauvegarde (EF-INT-015, ENF-OPS-001)**

| Aspect | Décision |
|---|---|
| Contenu du jeu | Snapshot `VACUUM INTO`, `config.toml`, manifestes d'export, clé maîtresse **chiffrée par une phrase de passe** |
| Destination | **Distincte** et configurable (autre disque, NAS, montage distant) |
| Rétention | Configurable (ex. 7 quotidiennes + 4 hebdomadaires) |
| Contrôle | `shelfhold backup verify` |
| Alertes | `backup.failed` et `backup.stale` via Apprise (EF-INT-006) |
| GC | Délai de grâce de la GC ≥ rétention |
| Avant migration | Une sauvegarde pré-migration nommée par révision, hors rotation |
| Restauration | `shelfhold backup restore`, puis `reconcile --adopt-after-restore`, qui recolle les fichiers dont le hash correspond |
| Test CI | Restauration sur un chemin différent |
| Objectifs | RPO 24 h, RTO 1 h |

**Export intégral (EF-INT-016)**
- Commande : `shelfhold export-all` produit un JSON versionné et documenté (schéma publié) accompagné d'un arbre de fichiers.
- Contenu : Œuvres, Séries, Éditions, provenance, verrous, progression, demandes, étagères, destinations sans secrets, utilisateurs sans hash de mot de passe.
- Réimport : l'import aller-retour dans une instance vierge est testé (état identique).
- Gouvernance : c'est la garantie de sortie promise par ENF-GOV-001.

## Conséquences
- Positives : récupération réelle après une panne de disque ou une perte de la clé ; pérennité garantie par un format ouvert ; confiance des utilisateurs auto-hébergés.
- Négatives : la phrase de passe est à conserver, et l'assistant impose son export (Q25). Le schéma JSON est à versionner et à garder compatible. Le temps de sauvegarde entre dans le budget nocturne (ENF-PERF-008).

## Risque principal
Une sauvegarde silencieusement corrompue ou incomplète. Mitigations : `backup verify` après chaque jeu, alerte `backup.stale` si aucun jeu valide depuis plus de 36 h, restauration testée en CI à chaque release.

## Réversibilité
Haute : formats et destinations sont de la configuration. Le schéma d'export ouvert est versionné et ne fait qu'ajouter.

## Amendement passe 2 (2026-11-02)
- **Périmètre** (B10, ADR-0028) : le jeu de sauvegarde est **`/data`** + manifestes + clé chiffrée ; `/store` est **hors** du jeu et documenté comme à sauvegarder par l'outil de l'hôte (comme Immich pour son dossier de téléversement).
- **RTO 1 h = remise en service**, pas restauration du contenu ; le contenu se restaure par ré-adoption de la bibliothèque Kavita et re-téléchargement ; restauration sans `/store` → aucune suppression, Unités `store_missing`.
- **Retour arrière** après migration : tag précédent + sauvegarde pré-migration, puis `reconcile --adopt-after-restore` (GAP-2-20) ; budget de migration ≤ 10 min sur le profil de référence.
- **Formats sur disque versionnés** (`schema_version`, ENF-OPS-013) ; **manifeste de fixité BagIt** avec l'export intégral en M4 (EF-BIB-031).

## Amendement passe 4 (2026-11-03)
Sauvegarde (D1, D13, GAP-4-17) : la sauvegarde intégrée arrive en **M1b** ; en M1a, la documentation fait sauvegarder `/data` par l'outil de l'hôte et `shelfhold secrets export` exporte la clé maîtresse chiffrée. La phrase de passe vient de `SHELFHOLD_BACKUP_PASSPHRASE_FILE` ; sans elle, aucune sauvegarde en clair n'est produite. EF-INT-015 est la source unique des règles ; la purge anticipée de la corbeille sous pression disque est une exception signalée.

## Amendement passe 5 (2026-11-03)
GAP-5-17. **M1a** : sauvegarde de `/data` par l'outil de l'hôte **une fois par jour** (RPO 24 h), Shelfhold arrêté après la fenêtre de maintenance (exemple documenté : 07 h 15). **Format d'export de la clé maîtresse** : `shelfhold secrets export --passphrase-file … --out …` écrit un fichier **age v1** (destinataire scrypt, armure ASCII), jamais en clair ni vers la sortie standard sans demande explicite ; c'est le même format que la clé incluse dans la sauvegarde intégrée de M1b ; `secrets import` le relit.

## Amendement passe 7 (2026-11-03)
G4, G7 (GAP-7-04, GAP-7-07).
- Toute restauration pose `instance.restore_epoch` ; une base en retard sur une sentinelle démarre en `restored_behind`, en lecture seule, jusqu'à `reconcile --adopt-after-restore` (ADR-0044, EF-INT-026). Les intentions de l'instantané ne sont jamais rejouées.
- `upstream_snapshot` (relevés des réglages amont) fait partie du jeu de sauvegarde ; il n'est jamais purgé sans détachement (EF-EXP-056).

## Amendement passe 9 (2026-11-03)
J2, J5 (GAP-9-07, GAP-9-08).
- `backup run` refuse une phrase de passe rangée dans l'arbre sauvegardé (`/data`), sauf `--accept-colocated-passphrase` journalisé ; `shelfhold status` l'avertit.
- Le jeu de sauvegarde et `secrets export` contiennent le **trousseau complet** : les clés retirées par une rotation restent au moins pendant la rétention des sauvegardes, sans quoi une copie antérieure ne se déchiffrerait plus.
- La rétention ne protège pas les copies gérées d'un contenu purgé (`purge --now`, ADR-0048) ; les sauvegardes intégrées ne contiennent pas les fichiers, et le rapport de purge liste celles qui citent encore le titre ; les sauvegardes de l'hôte restent hors de portée.

## Amendement passe 10 (2026-11-03)
K7 (Y4).
- Chaque release publie `schema.sql` et un dictionnaire de données générés ; ils entrent dans le jeu de sauvegarde (M1a : copie de l'hôte ; M1b : sauvegarde intégrée), pour qu'une base retrouvée se lise avec `sqlite3` seul, même si l'image a disparu.
- `store/INDEX.jsonl` (M1b), régénérable, réécrit dans la fenêtre de maintenance, rend `/store` lisible sans `/data` (sauvegardes séparées).
- Export des suivis en OPML et CSV (M3) pour quitter le projet sans outil ; ce n'est pas un contrat.
- `export-all` exclut les contenus sous pierre tombale et n'emporte aucune pierre tombale qui nommerait un titre (ADR-0048).

## Exigences liées
EF-INT-014, EF-INT-015, EF-INT-016, ENF-OPS-001, ENF-OPS-002, ENF-OPS-006, ENF-SEC-005, ENF-GOV-001, ENF-PERF-008, ENF-SEC-005, EF-INT-026, EF-EXP-056, EF-BIB-052, EF-INT-027.

## Sources
- https://docs.paperless-ngx.com/administration/#exporter (`document_exporter` / `document_importer`)
- https://www.sqlite.org/lang_vacuum.html#vacuuminto
- https://www.sqlite.org/backup.html
- https://immich.app/docs/administration/backup-and-restore
- `deliverables/review/contradicteur-ops-secu-legal.md` (OBJ-OPS-03), `chasseur-trous-passe1.md` (GAP-1-02, -03), ARBITRAGES A22
- `deliverables/review/chasseur-trous-passe4.md` (GAP-4-17)
- `deliverables/review/ARBITRAGES-passe4.md`
- https://github.com/C2SP/C2SP/blob/main/age.md
- `deliverables/review/chasseur-trous-passe5.md`
- `deliverables/review/explorateur-passe5.md`
- `deliverables/review/ARBITRAGES-passe5.md`
- `deliverables/review/chasseur-trous-passe7.md`, `deliverables/review/explorateur-passe7.md`, `deliverables/review/ARBITRAGES-passe7.md` (passe 7)
- `deliverables/review/chasseur-trous-passe9.md`, `deliverables/review/explorateur-passe9.md`, `deliverables/review/ARBITRAGES-passe9.md` (passe 9)
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)

## Amendement passe 12 (2026-11-03)
M8 (GAP-12-08).

- Le kit de secours imprimable ne contient **aucune clé** : il référence l'export chiffré du trousseau (`secrets export`, fichier séparé protégé par la phrase de passe), dit où le ranger et rappelle de le refaire après chaque rotation. Un `kid` cité par une pierre tombale reste dans le trousseau au-delà de la rétention des sauvegardes (ADR-0048).

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)
