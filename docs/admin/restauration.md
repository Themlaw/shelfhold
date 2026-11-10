# Sauvegarder et restaurer (M1a)

Depuis M1b, Shelfhold sauvegarde `/data` seul : voir [sauvegarde.md](sauvegarde.md) (destination
distincte, rétention de 14, alerte d'échec). Cette page dit ce qu'il faut sauvegarder
vous-même, à quelle fréquence, et comment repartir d'une copie.

## Objectifs

- **RPO 24 h** : une sauvegarde par jour au moins de `/data`.
- **RTO 1 h** pour la remise en service (pas pour recopier le contenu).

## Quoi sauvegarder

| Élément | Contenu | Comment |
| --- | --- | --- |
| `/data` | base SQLite, configuration, `meta/`, journal des pierres tombales | arrêtez l'instance puis copiez le dossier, ou utilisez un instantané du système de fichiers (ZFS, btrfs, LVM). **Ne copiez pas une base en marche avec `cp`.** |
| Clé maîtresse | `/data/keyring`, qui scelle tous les secrets de la base | **export obligatoire** : `shelfhold secrets export` |
| `/store` | copies conservées (vide tant que vous n'en gardez pas) | outil de sauvegarde de l'hôte |
| `/library` | votre bibliothèque | votre sauvegarde habituelle de Kavita : Shelfhold n'en est pas le propriétaire |

### Pour relire la base sans Shelfhold (EF-INT-027)

Chaque release publie en assets deux fichiers générés des migrations : **`schema.sql`** (le schéma
SQLite complet) et **`data-dictionary.md`** (tables, colonnes, valeurs sentinelles, formats écrits
sur disque et leurs versions). **Rangez-les avec votre jeu de sauvegarde**, à côté de la copie de
`/data` : en M1a c'est vous qui les copiez (la sauvegarde intégrée, qui les inclura d'elle-même,
arrive en M1b). Une base retrouvée des années plus tard se relit alors avec `sqlite3` seul :

```sh
sqlite3 vide.db < schema.sql       # schéma vide dans un fichier à part, pour comparer
sqlite3 -readonly shelfhold.db '.tables'
```

Prenez ceux de la version qui a écrit la base : la table `alembic_version` donne sa révision de
schéma. Voir [releasing.md](../releasing.md).

La base est l'autorité ; les fichiers `unit.json` sont des projections régénérables
(`shelfhold meta rebuild`).

## Exporter la clé (une fois, puis à chaque rotation)

Sans la clé, une base restaurée ne peut plus déchiffrer ses secrets (comptes Kavita et Suwayomi,
notifications) ni lire les pierres tombales. Instance arrêtée :

Le fichier de phrase de passe (`export.pass`) est écrit par vous, appartient au compte qui lance
Shelfhold (uid 1000) et est en `chmod 600` ; un fichier lisible par d'autres, un lien symbolique
ou un fichier de `/proc` est refusé.

```sh
chmod 600 /data/export.pass
docker compose stop shelfhold
docker compose run --rm shelfhold shelfhold secrets export \
    --passphrase-file /data/export.pass --out /data/keyring.age
docker compose start shelfhold
```

Le fichier est au format **age** (phrase de passe), lisible avec l'outil `age` standard. Gardez-le
**hors de l'hôte** et la phrase de passe **ailleurs que lui**. `shelfhold secrets status` signale
un trousseau absent ou aux droits trop ouverts ; il ne sait pas si vous l'avez exporté. Voir aussi [secrets.md](secrets.md).

## Restaurer

1. Arrêtez Shelfhold. Remettez `/data` à son état sauvegardé (et le trousseau, s'il n'y est pas).
2. Redémarrez. Au démarrage, Shelfhold compare la base aux **sentinelles** que chaque racine de
   `/library` porte. Si une sentinelle est plus récente que la base, c'est qu'on a restauré en
   arrière : la **époque de restauration** est incrémentée, l'événement `instance.restored_behind`
   est émis et l'instance passe en **lecture seule** (`restored_behind`).
3. Dans cet état : aucune écriture, suppression, session d'écriture ni scan, sur aucune racine ; les
   tâches périodiques sont suspendues. Les intentions de fichier issues de la sauvegarde ne sont
   **jamais rejouées** (`stale_from_backup`).
4. Regardez le rapport, sans rien écrire :
   ```sh
   docker compose stop shelfhold
   docker compose run --rm shelfhold shelfhold reconcile --adopt-after-restore --dry-run
   ```
   Il liste les fichiers rangés depuis la sauvegarde, les intentions périmées, les unités
   absentes, la dérive amont (séries suivies dans Suwayomi que la base restaurée ignore) et ce
   que couvre une pierre tombale. Il donne une **empreinte**.
5. Confirmez :
   ```sh
   docker compose run --rm shelfhold shelfhold reconcile --adopt-after-restore --confirm <empreinte>
   ```
   Le journal de pierres tombales est rejoué **avant** toute réadoption. Les fichiers présents
   sont réadoptés depuis le miroir de provenance de chaque racine (les décisions « ne plus suivre »
   reviennent, les âges ne reviennent que comme suggestions), puis les sentinelles sont ré-estampillées.
   **Rien n'est supprimé.** Si la clé d'un journal est inconnue ou si le journal est illisible,
   la réadoption est bloquée et rien n'est dégelé : retrouvez le trousseau, ou
   `tombstone accept-lost-keyring` ([etats-attente-et-sorties.md](etats-attente-et-sorties.md)).
6. Si la restauration doit faire disparaître des fichiers recouverts par une pierre tombale (ce que
   vous aviez purgé après la sauvegarde), ils sont **proposés** à la suppression, pas supprimés.
   Ajoutez `--confirm-deletion` pour les supprimer ; c'est une action réservée
   ([purge.md](purge.md)) qui passe par la même procédure de purge, avec audit. Si l'un de ces
   fichiers couvre un chapitre **irremplaçable** (aucune source ne peut le rendre, ou la base
   restaurée ne le connaît pas), l'aperçu le dit et la suppression exige en plus de **retaper
   l'identifiant de chaque chapitre** : `--confirm-unit <identifiant>` (répétable). Sans cela rien
   n'est modifié (code de sortie 2).
7. Démarrez : `docker compose start shelfhold`.

## Retour à une image antérieure

Une base migrée vers un schéma plus récent ne se relit pas avec l'ancienne image : Shelfhold refuse
de démarrer sur un schéma plus récent que le sien. Pour revenir en arrière, restaurez la sauvegarde
prise **avant** la migration (une sauvegarde est faite automatiquement par `shelfhold migrate`, nommée par
révision, dans `/data/backups/pre-migration`), redéployez l'image précédente, puis la procédure ci-dessus
(`restored_behind`). Les acquisitions postérieures à la sauvegarde sont signalées, jamais
supprimées. Voir [redeploiement.md](redeploiement.md).

## Ce que Shelfhold ne sauvegarde pas

Cette liste est aussi celle de l'effacement ([purge.md](purge.md)) : les sauvegardes de l'hôte,
les caches et signets de Kavita, les copies faites hors de Shelfhold et les journaux de l'hôte sont
hors de sa portée.
