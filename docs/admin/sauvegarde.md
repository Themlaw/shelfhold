# Sauvegarde intégrée : lancer, vérifier, restaurer (M1b)

Depuis M1b, Shelfhold sauvegarde `/data` tout seul, chaque nuit, vers un dossier **distinct** de
`/data` et de `/store`. Cette page est la source des règles ; [restauration.md](restauration.md) dit
quoi sauvegarder d'autre (`/store`, la bibliothèque) avec l'outil de votre hôte.

## Mise en place

| Réglage (variable) | Rôle |
| --- | --- |
| `SHELFHOLD_BACKUP_DIR` | dossier monté, hors de `/data` et de `/store` (ni dedans, ni autour). Un avertissement `status` apparaît s'il est sur le même disque que `/data`. |
| `SHELFHOLD_BACKUP_PASSPHRASE_FILE` | fichier (droits `0600`, 12 caractères au moins) qui contient la phrase de passe. Jamais en argument de commande. |
| `backup_retention` (14) | nombre de sauvegardes gardées. |
| `backup_stale_hours` (36) | au-delà, `backup.stale` et un avertissement dans `shelfhold status`. |

**Rangez le fichier de phrase de passe ailleurs que dans `/data`, et ailleurs que dans le dossier
des sauvegardes** : qui vole la sauvegarde aurait aussi la phrase. `backup run` le refuse
(`passphrase_colocated`) sauf avec `--accept-colocated-passphrase`; ce choix est journalisé et laisse
un avertissement durable dans `shelfhold status` tant qu'il dure. Pour un hôte distant, le dossier est
copié par l'outil de l'hôte (rsync, restic) : Shelfhold n'a pas de cible réseau pour des fichiers.

## Ce que contient une sauvegarde

Une archive `shelfhold-backup-<UTC>.tar.age`, **entièrement chiffrée** (age v1, scrypt : le format de
`shelfhold secrets export`, donc `age -d -p fichier | tar x` suffit à l'ouvrir sans Shelfhold) :
la base (`VACUUM INTO`, copie cohérente, `integrity_check` exigé), `config.toml`, `meta/`, le
**trousseau complet** (clés retirées comprises, lui-même un export age que `secrets import` relit),
`schema.sql` et le dictionnaire de données, un manifeste avec l'empreinte `sha256` de chaque membre.
`upstream_snapshot` est dans la base. Les **miroirs de provenance** des racines
(`<racine>/.shelfhold/manifest/`) n'y sont qu'avec `shelfhold backup run --with-manifests`: la tâche
planifiée ne réveille aucun disque de la bibliothèque, et ces fichiers se réécrivent depuis la base
(`shelfhold meta rebuild`).

Le jeu est tenu en mémoire pendant le chiffrement : au-delà de 1 Gio la sauvegarde est refusée
(`too_large`). Le seul octet en clair qu'une exécution produit est la copie `VACUUM INTO`, dans un
sous-dossier `0700` du dossier de sauvegarde, lue puis supprimée dans tous les cas (une exécution tuée
laisse un `.work-*` que la suivante supprime). Il faut, sur la destination, **deux fois la taille de la
base** en espace libre.

## Quand

`backup.run`, chaque jour à 01 h 40 (± 5 min), hors de la fenêtre de maintenance : elle ne touche ni
`/store` ni `/library`. Elle tient le verrou de maintenance : un `VACUUM` en cours la fait attendre
(10 min au plus, puis `backup.failed` cause `busy`), et aucun `VACUUM` ne démarre sous elle, y compris
quand `shelfhold backup run` est lancé depuis l'hôte (verrou de fichier `/data/.maintenance.lock`).
La plus ancienne sauvegarde n'est supprimée **qu'après** qu'une nouvelle a été écrite, relue et
vérifiée.

## Commandes

```sh
shelfhold backup run [--accept-colocated-passphrase] [--with-manifests]
shelfhold backup verify [--file archive.tar.age] [--passphrase-file f]   # déchiffre, vérifie chaque membre
shelfhold backup restore --from archive.tar.age --to /nouveau/dossier [--passphrase-file f]
```

`verify` ne restaure rien : il déchiffre, contrôle l'empreinte de chaque membre, ouvre le trousseau et
lance `PRAGMA integrity_check` sur la base en mémoire. Une archive tronquée, altérée ou une mauvaise
phrase de passe donnent **la même phrase** (volontairement).

## Restaurer

1. `shelfhold backup restore --from … --to /nouveau/data` : le dossier n'existe pas ou est vide, et
   n'est ni `/data` ni dedans. Rien n'est écrit avant que toute l'archive soit déchiffrée et vérifiée;
   un nom d'entrée avec `..`, absolu, ou un lien est refusé; rien n'est jamais supprimé.
2. Démarrez Shelfhold avec `SHELFHOLD_DATA_DIR=/nouveau/data`. Si une racine de la bibliothèque est en
   avance sur la base, l'instance démarre en **`restored_behind`** (lecture seule).
3. `shelfhold reconcile --adopt-after-restore` réadopte les fichiers. **Sans `/store`** (perdu ou pas
   encore monté), les Unités dont la copie conservée n'y est pas passent en `store_missing`; aucune
   suppression n'a lieu. Restaurez `/store` avec l'outil de l'hôte, ou `store reinit`.

RPO 24 h; RTO 1 h = remise en service, pas recopie du contenu.
