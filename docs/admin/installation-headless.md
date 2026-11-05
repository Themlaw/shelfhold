# Installer Shelfhold sans interface

Du compose au premier chapitre rangé dans Kavita, sans interface web et sans SQL. Cette page est
l'ordre des opérations ; les options de chaque commande, le fichier déclaratif `shelfhold.toml` et
les routes sont dans [headless.md](headless.md).

## Avant de commencer

- Docker avec Compose, un Kavita (0.9.1 ou plus récent) et un Suwayomi déjà en marche, joignables
  depuis le conteneur de Shelfhold (même réseau Docker, par exemple).
- Deux dossiers sur l'hôte, accessibles en écriture à l'uid 1000 : `data` (base, trousseau,
  configuration ; disque local, jamais NFS) et `store` (copies conservées, volume distinct).
- Un compte Kavita **administrateur** dédié à Shelfhold. Shelfhold l'échange contre une clé nommée
  à expiration et ne conserve pas le mot de passe.
- La bibliothèque Kavita de destination, de type Manga, vue par Shelfhold sous `/library`.

## Pas à pas

1. **Récupérer le compose** : [`compose.yaml`](../../compose.yaml) du dépôt. Il épingle l'image sur
   la ligne `0.2` ; ajoutez le montage de la bibliothèque (par exemple
   `/srv/media/manga:/library/manga`, le même dossier que celui de Kavita).
2. **Préparer les dossiers** :
   ```sh
   mkdir -p data store && chown 1000:1000 data store && chmod 750 data store
   ```
3. **Démarrer** : `docker compose up -d`. Le service `shelfhold-migrate` applique les migrations
   puis s'arrête ; `shelfhold` démarre ensuite. `curl http://127.0.0.1:8080/readyz` doit répondre.
4. **Exporter la clé maîtresse**, tout de suite (voir [restauration.md](restauration.md)) :
   ```sh
   docker compose stop shelfhold
   docker compose run --rm shelfhold shelfhold secrets export \
       --passphrase-file /data/export.pass --out /data/keyring.age
   docker compose start shelfhold
   ```
   Copiez `keyring.age` hors de l'hôte, la phrase de passe ailleurs.
5. **Configurer** avec les commandes de [headless.md](headless.md) (`admin create`, `config set
   public_url`, `target add kavita`, `source add suwayomi`, `shelf create`), ou en un fichier avec
   `shelfhold config apply -f shelfhold.toml --dry-run` puis sans `--dry-run`. Lancées pendant que
   le serveur tourne, elles passent par la socket `/data/shelfhold.sock`
   (`docker compose exec shelfhold shelfhold …`).
6. **Vérifier** : `shelfhold status` ne doit rien lister qui attende. Voir
   [etats-attente-et-sorties.md](etats-attente-et-sorties.md) sinon.
7. **Premier chapitre** : le suivi tourne tout seul (mode `always_on`, EF-SRC-025). Dès qu'un
   chapitre d'une série suivie paraît, il traverse le staging, est rangé dans la bibliothèque, puis
   Kavita le voit au scan. S'il n'apparaît pas dans Kavita, l'état `not_indexed` le dit avec la
   cause probable (exclusion de dossier, type de bibliothèque).

## Si vous avez déjà une bibliothèque

N'ajoutez pas d'étagère par-dessus : suivez d'abord [adopter-kavita.md](adopter-kavita.md), et si
Suwayomi écrit déjà dans les dossiers de Kavita, [migration-depuis-scripts.md](migration-depuis-scripts.md).

## Au passage

- Rien n'est jamais passé en argument de ligne de commande : mots de passe et secrets se lisent
  dans un fichier `chmod 600` ou sur l'entrée standard.
- Shelfhold ne réécrit pas ce qu'il n'a pas écrit. Aucune étape ci-dessus ne modifie un fichier
  existant de votre bibliothèque.
