# Sauvegarde intégrée : lancer, vérifier, restaurer (M1b)

Depuis M1b, Shelfhold sauvegarde `/data` tout seul, chaque nuit, vers un dossier **distinct** de
`/data` et de `/store`. Cette page est la source des règles ; [restauration.md](restauration.md) dit
quoi sauvegarder d'autre (`/store`, la bibliothèque) avec l'outil de votre hôte.

## Mise en place

| Réglage (variable) | Rôle |
| --- | --- |
| `SHELFHOLD_BACKUP_DIR` | dossier monté, hors de `/data` et de `/store` (ni dedans, ni autour). Un avertissement `status` apparaît s'il est sur le même disque que `/data`. |
| `SHELFHOLD_BACKUP_PASSPHRASE_FILE` | fichier (droits `0600`, 12 caractères au moins) qui contient la phrase de passe. Jamais en argument de commande. |
| `backup_retention` (14) | nombre de sauvegardes gardées (1 à 30). |
| `backup_stale_hours` (36) | au-delà, `backup.stale` et un avertissement dans `shelfhold status`. |

**Rangez le fichier de phrase de passe ailleurs que dans `/data`, et ailleurs que dans le dossier
des sauvegardes** : qui vole la sauvegarde aurait aussi la phrase. `backup run` le refuse
(`passphrase_colocated`), par le chemin **et** par l'identité du fichier (un lien dur ou un montage
« bind » vers `/data` n'y échappe pas), sauf avec `--accept-colocated-passphrase`; ce choix est journalisé et laisse
un avertissement durable dans `shelfhold status` tant qu'il dure. Pour un hôte distant, le dossier est
copié par l'outil de l'hôte (rsync, restic) : Shelfhold n'a pas de cible réseau pour des fichiers.

## Ce que contient une sauvegarde

Un fichier `shelfhold-backup-<UTC>.tar.age`, **entièrement chiffré**, jamais une donnée en clair
sur la destination. Il contient la base (`VACUUM INTO`, copie cohérente, `integrity_check`
exigé), `config.toml`, `meta/`, le **trousseau complet** (clés retirées comprises, lui-même un
export age que `secrets import` relit), `schema.sql` et le dictionnaire de données, et un
manifeste avec l'empreinte `sha256` de chaque membre. `upstream_snapshot` est dans la base. Les
**miroirs de provenance** des racines (`<racine>/.shelfhold/manifest/`) n'y sont qu'avec
`shelfhold backup run --with-manifests` : la tâche planifiée ne réveille aucun disque de la
bibliothèque, et ces fichiers se réécrivent depuis la base (`shelfhold meta rebuild`).

### Format et déchiffrement avec le `age` standard

Le fichier est : une première ligne `shelfhold-backup/2 <N>`, puis `N` octets (une clé age
éphémère, chiffrée par votre phrase de passe : scrypt), puis le tar, chiffré age vers la clé
publique de cette clé. Chaque sauvegarde a sa propre clé ; la phrase de passe protège la clé, la
clé protège le contenu. Sans Shelfhold, en deux étapes :

```sh
umask 077                                # la clé éphémère ne doit être lisible que de vous
F=shelfhold-backup-20261108T014000Z.tar.age
N=$(head -n 1 "$F" | cut -d' ' -f2)        # taille de la clé scellée
L=$(head -n 1 "$F" | wc -c)                # taille de la première ligne
tail -c +$((L + 1)) "$F" | head -c "$N" | age -d > cle-ephemere.txt   # demande la phrase de passe
tail -c +$((L + N + 1)) "$F" | age -d -i cle-ephemere.txt | tar x -C /ou/vous/voulez
shred -u cle-ephemere.txt
```

Le tar contient `BACKUP.json` (manifeste), `shelfhold.db`, `keyring.age` (relisible par
`shelfhold secrets import`), `config.toml`, `meta/`, `docs/`.

### Coût de la phrase de passe

Shelfhold écrit lui-même l'enveloppe age de la phrase de passe (la clé éphémère de la sauvegarde et
`keyring.age`) avec un facteur scrypt **fixe**, `log_n = 18` : 256 Mio de mémoire un court instant
(128 x 8 x 2^18 octets, le réglage par défaut d'age), ce qui tient dans le budget de 500 Mo. Aucune
mesure de la machine n'y entre : une sauvegarde faite sur un hôte au repos s'ouvre sur un hôte
chargé ou plus lent, ce qui est la situation d'une restauration après sinistre. À la lecture, la
borne est `log_n <= 20` (le `age` standard ouvre toujours le fichier) ; au-delà, la commande dit que
le coût est excessif (`backup.too_costly`), pas que la phrase de passe est fausse. Les sauvegardes
et exports faits avant ce réglage (coût calibré par age) restent lisibles.

### Mémoire et taille

Tout est traité **en flux** : l'écriture, `verify` et `restore` n'ont pas besoin de place en
mémoire pour la base, quelle que soit sa taille (mesuré : moins de 5 Mio de plus que le socle pour
64 Mio de base ; le banc nocturne le refait sur 320 Mio). Le seul plafond est celui d'un tar de
32 Gio, qui coupe un flux sans fin. Reste le coût des couches « phrase de passe » (scrypt) :
256 Mio un court instant, fixes, indépendants de la taille de la sauvegarde.

### Où est le seul clair

L'instantané `VACUUM INTO` est créé dans `/data/tmp/backup-*` (mode 0700, même système de fichiers
que la base, qui y est déjà en clair), jamais dans le dossier de sauvegarde, qu'un `rsync` ou un
`restic` copie vers l'extérieur et qui peut être sur un système de fichiers sans droits (FAT, CIFS,
NFS). Il est supprimé dans tous les cas ; après un arrêt brutal, le dossier orphelin est supprimé
au démarrage suivant et au début de la sauvegarde suivante. Il faut, sur `/data`, **deux fois la
taille de la base** en espace libre.

## Quand

`backup.run`, chaque jour à 01 h 40 (± 5 min), hors de la fenêtre de maintenance : elle ne touche ni
`/store` ni `/library`. Elle tient le verrou de maintenance : un `VACUUM` en cours la fait attendre
(10 min au plus, puis `backup.failed` cause `busy`), et aucun `VACUUM` ne démarre sous elle, y compris
quand `shelfhold backup run` est lancé depuis l'hôte (verrou de fichier `/data/.maintenance.lock`).
Deux sauvegardes ne tournent jamais ensemble (verrou `/data/.backup.lock` ; la seconde échoue avec la cause `locked`, distincte de `busy`, qui est un `VACUUM` en cours). La plus ancienne
sauvegarde n'est supprimée **qu'après** qu'une nouvelle a été écrite, relue et vérifiée, et la
rétention ne supprime que les archives que cette instance a elle-même écrites et vérifiées
(lignes `backup_run`) : un fichier ou un dossier étranger dans le dossier de sauvegarde n'est
jamais touché. Une exécution laissée « en cours » par un arrêt brutal passe à `interrupted`
(avec `backup.failed`) au démarrage.

## Commandes

```sh
shelfhold backup run [--accept-colocated-passphrase] [--with-manifests]
shelfhold backup verify [--file archive.tar.age] [--passphrase-file f]   # déchiffre, vérifie chaque membre
shelfhold backup restore --from archive.tar.age --to /nouveau/dossier [--passphrase-file f]
```

`verify` ne restaure rien : il déchiffre, contrôle l'empreinte de chaque membre, ouvre le trousseau et
lance `PRAGMA integrity_check` sur la base (écrite un instant dans `/data/tmp`, jamais sur la destination). Une archive tronquée, altérée ou une mauvaise
phrase de passe donnent **la même phrase** (volontairement).

## Restaurer

1. `shelfhold backup restore --from … --to /nouveau/data` : le dossier n'existe pas ou est vide, et
   n'est ni `/data` ni dedans. Rien n'est écrit avant qu'une première passe ait déchiffré et vérifié
   toute l'archive ; une seconde la réécrit en flux, empreinte contrôlée de nouveau, la base en
   dernier puis contrôlée là où elle atterrit. Un nom d'entrée avec `..`, absolu, ou un lien est
   refusé ; rien n'est jamais supprimé. Si l'instance garde son trousseau ailleurs
   (`SHELFHOLD_SECRET_KEY_FILE`), aucun trousseau n'est recréé dans le nouveau dossier.
   **Les identifiants sont révoqués dans la base restaurée**, avant qu'elle ne prenne son nom : toutes
   les sessions et toutes les clés d'API, y compris celles qui étaient valides le jour de la
   sauvegarde. Sans cela, une clé révoquée après la sauvegarde (ou une session fermée après une
   compromission) serait de nouveau valide. La commande dit combien en ont été fermées ; la ligne
   d'audit `restore.credentials_revoked` le garde. Les clés se recréent (`shelfhold admin keys
   create`) et on se reconnecte. L'avertissement est écrit dans la base elle-même, dans la même
   transaction : il est **durable** (voir plus bas).
2. Démarrez Shelfhold avec `SHELFHOLD_DATA_DIR=/nouveau/data`. Si une racine de la bibliothèque est en
   avance sur la base, l'instance démarre en **`restored_behind`** (lecture seule).
3. `shelfhold reconcile --adopt-after-restore` réadopte les fichiers. **Sans `/store`**, on
   distingue deux cas par le fichier `.shelfhold-mount-id` de `/store` : absent (volume pas monté ou
   perdu), l'état est `store_lost` et **rien n'est marqué** (montez `/store`, ou `store reinit`) ;
   présent mais une copie absente, l'Unité passe en `store_missing`. Dans les deux cas aucune
   suppression n'a lieu ; une copie qui revient avec la bonne empreinte remet l'Unité en `stored`.

### Après la restauration : passkeys et mots de passe

Tant que l'avertissement est là, **la connexion par passkey est refusée pour tous les comptes**
(ainsi que la preuve de ré-authentification par passkey) : une passkey révoquée depuis la sauvegarde
serait sinon de nouveau valide.

- Si `backup restore` peut lire la base en service (`/data` d'origine encore là), il en reporte les
  révocations de passkeys et les empreintes de mots de passe : un `admin reset-password` fait depuis
  la sauvegarde est conservé. Les comptes **supprimés depuis la sauvegarde** reviennent sans mot de
  passe et avec leurs passkeys révoquées ; l'avertissement les nomme. Il liste aussi les passkeys
  créées ou révoquées depuis la sauvegarde.
- Sinon (base remise à la main, ou `/data` d'origine perdu), les mots de passe changés depuis la
  sauvegarde **sont revenus à l'ancienne valeur**, peut-être une valeur qui a fuité. **La connexion
  par mot de passe est alors suspendue aussi** (409 `password_login_suspended`).

**Se reconnecter après une restauration** (aucune session HTTP ne peut lever l'avertissement) :
sur la machine hôte, avec la CLI locale (socket ops locale),

```sh
shelfhold admin reset-password <compte>   # pour chaque compte, si les mots de passe n'ont pas été reportés
shelfhold status                          # liste `restored_credentials`, les passkeys et comptes concernés
shelfhold restore acknowledge             # CLI de l'hôte uniquement ; après `restore adopt` si la restauration a aussi gelé des racines : ni session, ni clé, jamais via HTTP
```

`restore acknowledge` liste les passkeys créées ou révoquées depuis la sauvegarde, rétablit les
connexions (passkey, et mot de passe s'il était suspendu) et est audité
(`restore.credentials_acknowledged`). `shelfhold status` affiche `restored_credentials` tant que ce
n'est pas acquitté, redémarrages compris.

### Compteur d'identité

Une base remise à la main n'est pas toujours « en retard » sur des fichiers (restauration sans
bibliothèque, ou sentinelles identiques). Shelfhold tient donc un **compteur d'identité monotone**
dans `/data/.identity-counter` (écrit de façon atomique, droits `0600`, **jamais sauvegardé**),
copié dans la base dans la même transaction. Chaque révocation (session, clé d'API, passkey) et
chaque changement de mot de passe l'augmente ; le fichier est écrit **en deux temps** : avant la validation
`committed=N` et `pending=N+1`, après la validation `committed=N+1`. Au démarrage, `committed` au-dessus
de la base = restauration (révocation) ; `committed` égal à la base avec un `pending` = écriture
interrompue (aucune révocation : le `pending` est effacé, ou adopté si la base l'a validé). Un ancien
fichier (un nombre seul) est lu comme `committed`. Avant toute écriture d'identité
et au démarrage, avant toute requête : si le fichier est au-dessus de la base, la base est plus
ancienne que l'état connu, toutes les sessions et clés d'API sont révoquées et l'avertissement est
posé (une commande lancée sur la copie avant le démarrage ne masque donc pas l'écart). Un fichier
vide ou illisible compte comme en avance (une révocation, puis il est réécrit) ; un **dossier** à sa
place refuse le démarrage avec un message clair ; une base verrouillée ou en erreur est retentée
puis arrête le démarrage. Pendant `instance.starting`, `/api/v1/auth/*` répond aussi 503.

Limite : si **tout `/data`** est remplacé, le fichier part avec la base ; il ne reste alors que la
détection par les sentinelles (`restored_behind`).

La même révocation a lieu quand une base copiée à la main est trouvée en retard sur ses fichiers
(`restored_behind`, première fois seulement), pour une restauration faite sans `backup restore`.

RPO 24 h; RTO 1 h = remise en service, pas recopie du contenu.
