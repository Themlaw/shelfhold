---
status: accepté
date: 2026-11-03
amended: 2026-11-03 (passe 8) ; 2026-11-03 (passe 10) ; 2026-11-03 (passe 11) ; 2026-11-03 (passe 12)
decision-makers: mainteneur
---
# ADR-0044 — Restauration et époques

## Contexte et problème
La sentinelle `.shelfhold-mount-id` porte un compteur de génération et bloque un dossier absent, différent ou **en retard** (ADR-0022). Après une restauration de `/data` (EF-INT-015, ou copie de l'hôte en M1a) ou un retour arrière d'image (ENF-OPS-008), c'est la **base** qui est en retard sur le disque : le cas n'avait pas de sens défini (GAP-7-04). Il pouvait bloquer toute écriture sans issue, ou au contraire laisser un manifeste périmé écrire. Un instantané pris en marche peut aussi contenir des `fs_intent` ouverts : au démarrage, ils auraient été « achevés ou annulés » sur un disque qui a changé depuis. Enfin, les réglages amont relevés à l'adoption étaient « consignés dans le journal d'intentions » (GAP-7-07), journal résolu au démarrage et soumis à rétention : un relevé purgé ne se reconstitue pas.

## Facteurs de décision
- La sentinelle et le journal d'intentions sont des intouchables de M1a : le sens de la comparaison doit être fixé avant le premier déploiement.
- Après une restauration, ne rien supprimer et ne rien rejouer à l'aveugle.
- Une seule sortie, documentée, testable de bout en bout.
- Un relevé durable ne doit pas dépendre d'un journal d'opérations.

## Options considérées
### A. Traiter la base en retard comme une sentinelle « différente »
- Avantages : aucun état nouveau.
- Inconvénients : blocage total sans commande de sortie propre ; l'admin est tenté de forcer.
### B. Ré-estampiller la sentinelle au démarrage
- Avantages : aucune attente.
- Inconvénients : un manifeste périmé écrit sur un disque plus récent ; intentions rejouées sur un disque changé ; pertes possibles.
### C. Époque de restauration, état `restored_behind` en lecture seule, intentions d'instantané jamais rejouées, relevés amont dans une table dédiée
- Avantages : comportement sûr et explicable ; une seule sortie (`reconcile --adopt-after-restore`) ; rien n'est perdu.
- Inconvénients : une action humaine après chaque restauration ; un état de plus à tester.

## Décision
Option C (arbitrages G4, G7).
- Toute restauration et tout retour arrière posent `instance.restore_epoch` (et `restore_generation`).
- Au démarrage, une base dont la génération est inférieure à celle d'une sentinelle passe l'instance en **`waiting_state = restored_behind`** : réconciliation en lecture seule et rapport (fichiers rangés depuis la sauvegarde, Unités absentes, intentions périmées) ; aucune écriture, suppression ni scan ; événement `instance.restored_behind` (EF-INT-026).
- Un `Fsops` ouvert tôt (un export qui attend son scan) garde la génération lue à son ouverture. Une sentinelle plus récente que cette copie n'est une restauration que si elle l'est aussi **de la base** : `Fsops` relit alors le compteur de la base (le journal des générations, que toute session d'écriture avance avant de réécrire la sentinelle) et suit la valeur qu'il y trouve, lue d'un seul `SELECT` avec l'époque et le gel (de l'instance ou de la racine) et filtrée par identifiant et chemin de la racine : un gel tenu en base, une époque différente ou une autre racine au même chemin interdisent le suivi. Une sentinelle que la base dépasse de plus d'un cran reste `stale` (cette racine seule, ADR-0022), sans geler l'instance. Une sentinelle au-delà de la base (sauvegarde restaurée), d'une autre racine ou installation, ou d'une époque de restauration plus récente reste `restored_behind`.
- Les intentions dont `created_at` est antérieur à l'époque passent **`stale_from_backup`** et ne sont **jamais rejouées** (invariant I28).
- Seule sortie : `shelfhold reconcile --adopt-after-restore` : réadoption par manifeste, chemin et `content_hash`, sans suppression ; acquisitions postérieures signalées ; sentinelle ré-estampillée.
- Les relevés amont vivent dans la table **`upstream_snapshot`** (`connected_service_id`, `series_ref`, `capability`, `payload`, `schema_version`, `taken_at`), sauvegardée, sans rétention automatique, jamais purgée sans détachement (invariant I31) ; le journal d'intentions ne porte que des références (EF-EXP-056).
- EF-INT-026 rejoint les intouchables de M1a (ADR-0035).

## Conséquences
- Positives : restauration et retour arrière sans perte ni réécriture à l'aveugle ; `detach --restore-upstream` fonctionne même longtemps après l'adoption.
- Négatives : une commande à lancer après chaque restauration ; `shelfhold status` doit l'expliquer clairement.

## Risque principal
Un admin qui restaure sans lire le rapport et réadopte des fichiers qu'il voulait écarter. Mitigation : le rapport liste les écarts avant la commande ; la réadoption ne supprime rien et reste réversible par la corbeille et le manifeste.

## Réversibilité
Haute : l'état et la table sont additifs ; la règle peut être assouplie plus tard, pas durcie sans risque, d'où son placement en M1a.

## Amendement passe 8 (2026-11-03)
H1 (GAP-8-01).
- `reconcile --adopt-after-restore` lit d'abord le **miroir de provenance** de chaque racine (ADR-0046) : un fichier dont le `file_hash` concorde reprend l'état `exported` avec sa Variante, son Équipe et son `export_item` ; sinon, appariement par hash et chemin, état `adopted`. Le miroir étant sur `/library`, il n'est pas en retard comme la base restaurée.

## Amendement passe 9 (2026-11-03)
J2, J5 (X2, GAP-9-03, GAP-9-04, GAP-9-05).
- L'époque est comparée par **compteur** : `fs_intent.epoch` remplace `fs_intent.created_at < restore_epoch` ; aucune décision ne dépend de l'horloge murale (I39). Une base en avance d'un cran sur la sentinelle est cohérente (ADR-0022).
- Le **journal de pierres tombales hors base** (`<racine>/.shelfhold/tombstones`, ADR-0048) est rejoué au démarrage et avant toute réadoption : une base `restored_behind` ne fait jamais réapparaître un contenu purgé (I36).
- Le rapport de `restored_behind` liste la **dérive amont** (séries de la catégorie Shelfhold de Suwayomi inconnues de la base) et propose, sans relevé, la restauration par défaut (retrait de la catégorie).
- Les lignes du miroir qui ne se résolvent pas restent `unresolved` (ADR-0046), jamais effacées.

## Amendement passe 10 (2026-11-03)
K2 (GAP-10-02).
- Le rejeu du journal de pierres tombales avant la réadoption **bloque** toujours ; il ne **supprime** qu'à partir de lignes vérifiées (MAC valide ou présentes dans la table) et, après une restauration, seulement après **confirmation de l'admin** sur la liste présentée (confirmation tapée pour une Unité irremplaçable). Les lignes non vérifiées figurent au rapport (`tombstone.unverified`). Invariant I41.

## Amendement passe 11 (2026-11-03)
L1. Au rejeu (démarrage, après restauration), une ligne de pierre tombale qui ne se déchiffre pas (clé inconnue ou perdue, ligne altérée, recopiée d'une autre racine ou forgée) est **inerte** : elle ne bloque ni ne supprime rien, émet `tombstone.unverified` et figure au rapport ; la passe 10 la faisait bloquer, ce qui n'est plus possible sans valeur lisible. Une ligne déchiffrée bloque ; une suppression issue du rejeu après restauration attend toujours la confirmation de l'admin. Le rejeu reconstruit l'index en mémoire et vérifie la troncature (M1b).

## Exigences liées
EF-INT-026, EF-EXP-029, EF-EXP-041, EF-EXP-056, EF-INT-015, ENF-OPS-008, EF-INT-023, EF-EXP-063, EF-BIB-052, EF-BIB-053, EF-BIB-054.

## Sources
- `deliverables/review/chasseur-trous-passe7.md` (GAP-7-04, GAP-7-07)
- `deliverables/review/ARBITRAGES-passe7.md` (G4, G7)
- https://www.sqlite.org/lang_vacuum.html#vacuuminto
- https://sqlite.org/wal.html
- `deliverables/review/chasseur-trous-passe8.md`, `deliverables/review/explorateur-passe8.md`, `deliverables/review/ARBITRAGES-passe8.md` (passe 8)
- `deliverables/review/chasseur-trous-passe9.md`, `deliverables/review/explorateur-passe9.md`, `deliverables/review/ARBITRAGES-passe9.md` (passe 9)
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)

## Amendement passe 12 (2026-11-03)
M1 (GAP-12-01).

- Une restauration de `/data` antérieure à une rotation du trousseau ne fait plus réapparaître un contenu purgé après la rotation : au rejeu en `restored_behind`, une ligne au `kid` inconnu **bloque** `reconcile --adopt-after-restore` et la réacquisition de sa portée sur sa racine (`tombstone.key_unknown`, alerte « réimporter le trousseau ») jusqu'à `secrets import` du dernier export ou la dérogation tapée « trousseau perdu » (ADR-0048). Test `test_restore_pre_rotation_backup_keeps_post_rotation_tombstones`.

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)

## Amendement passe 13 (2026-11-03)
N1 (GAP-13-01).

- Au rejeu après restauration, une ligne au `kid` absent du trousseau ne bloque que si ce `kid` figure au registre des `kid` émis (base ou sentinelle de sa racine) ; une base restaurée d'avant la rotation retrouve le `kid` dans la sentinelle, qui n'est pas restaurée avec `/data`. Une ligne au `kid` absent du registre est forgée et inerte.

Source : `deliverables/review/chasseur-trous-passe13.md`, `deliverables/review/explorateur-passe13.md`, `deliverables/review/ARBITRAGES-passe13.md` (passe 13)
