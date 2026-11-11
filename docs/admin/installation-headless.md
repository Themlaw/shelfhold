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
   une ligne `X.Y` (`0.2` aujourd'hui, `0.3` à la publication de la 0.3.0 : tant qu'elle n'existe pas,
   gardez `0.2` ou prenez `edge`).
   Ajoutez le montage de la bibliothèque (par exemple `/srv/media/manga:/library/manga`, le même
   dossier que celui de Kavita) : le compose fourni ne monte que `/data` et `/store`.
2. **Préparer les dossiers** :
   ```sh
   mkdir -p data store && chown 1000:1000 data store && chmod 750 data store
   ```
3. **Démarrer** : `docker compose up -d`. Le service `shelfhold-migrate` applique les migrations
   puis s'arrête ; `shelfhold` démarre ensuite. `curl http://127.0.0.1:8080/readyz` doit répondre.
4. **Exporter la clé maîtresse**, tout de suite (voir [restauration.md](restauration.md)). La
   phrase de passe est un fichier que vous écrivez vous-même (`data/export.pass` sur l'hôte,
   `chmod 600`) :
   ```sh
   docker compose stop shelfhold
   docker compose run --rm shelfhold shelfhold secrets export \
       --passphrase-file /data/export.pass --out /data/keyring.age
   docker compose start shelfhold
   ```
   Copiez `keyring.age` hors de l'hôte, la phrase de passe ailleurs, puis supprimez
   `data/export.pass` de l'hôte.
5. **Configurer** avec les commandes de [headless.md](headless.md), dans cet ordre :
   `target add kavita` (qui s'arrête sur l'avertissement « compte administrateur » tant que
   `--accept-admin-risk` n'est pas donné, sans rien envoyer à Kavita), `source add suwayomi`,
   `shelf create`. `admin create` et `config set public_url` ne sont utiles que pour se connecter
   à l'API. Ou en un fichier : `shelfhold config apply -f shelfhold.toml --dry-run` puis sans
   `--dry-run`. Lancées pendant que le serveur tourne, ces commandes passent par la socket
   `/data/shelfhold.sock` (`docker compose exec shelfhold shelfhold …`). Le conteneur est en
   lecture seule : déposez les fichiers de secrets dans `data/` (visibles sous `/data`) ou lisez-les
   sur l'entrée standard (`--password-file -` avec `exec -T`), et supprimez-les ensuite.
6. **Vérifier** : `shelfhold config list` montre les clés posées (dont `path_map.kavita`) et
   `shelfhold status` ne doit rien lister qui attende. Voir
   [etats-attente-et-sorties.md](etats-attente-et-sorties.md) sinon.
7. **Premier chapitre** : le suivi tourne tout seul (mode `always_on`, EF-SRC-025), toutes les
   15 minutes (avec un décalage de 2 minutes au plus). Pour ne pas attendre,
   `shelfhold jobs run follow.cycle` le lance au prochain passage (une minute au plus). Un
   chapitre qui paraît ensuite en amont traverse le staging, est rangé dans la bibliothèque, puis
   Kavita le voit au scan, après environ 30 s de calme (EF-EXP-034). S'il n'apparaît pas dans
   Kavita, l'état `not_indexed` le dit avec la cause probable (exclusion de dossier, type de
   bibliothèque).

   Le suivi ne porte que sur les séries **liées**, et Shelfhold les lie lui-même : au début de
   chaque cycle, il découvre les séries de la bibliothèque Suwayomi qui n'ont pas encore de liaison
   et leur crée une Œuvre, une Édition sur votre étagère adulte et la liaison. Le cycle qui
   découvre une série enregistre ce que Suwayomi en montre déjà **sans rien demander** : seuls les
   chapitres publiés après lui sont récupérés. Trois cas à connaître :

   - **pas d'étagère adulte** (une étagère 18+ liée à une bibliothèque Kavita) : rien n'est créé,
     la notification `series.awaiting_shelf` donne la commande (`shelfhold shelf create`) et
     `shelfhold status` la liste en `awaiting_shelf` ; la découverte reprend au cycle suivant ;
   - **série déjà adoptée depuis Kavita** : elle est laissée à
     `shelfhold adopt pair --series <id> --source-key <clé> --remote-id <id>` (ou
     `shelfhold adopt --pair-auto`) tant que son appariement attend, et `shelfhold status` la liste
     en `pending_pairing` ; `adopt pair --series <id> --decline` dit qu'elle n'a pas de jumelle
     dans Suwayomi ;
   - **sous-source sans langue unique** (`all`) : la série est créée, son Édition rangée en langue
     inconnue (`x-unknown`) ; `shelfhold status` la liste en `language_unknown` jusqu'à
     `shelfhold edition set-lang <édition> <langue>`.

## Si vous avez déjà une bibliothèque

N'ajoutez pas d'étagère par-dessus : suivez d'abord [adopter-kavita.md](adopter-kavita.md), et si
Suwayomi écrit déjà dans les dossiers de Kavita, [migration-depuis-scripts.md](migration-depuis-scripts.md).

## Au passage

- Rien n'est jamais passé en argument de ligne de commande : mots de passe et secrets se lisent
  dans un fichier `chmod 600` ou sur l'entrée standard.
- Shelfhold ne réécrit pas ce qu'il n'a pas écrit. Aucune étape ci-dessus ne modifie un fichier
  existant de votre bibliothèque.
