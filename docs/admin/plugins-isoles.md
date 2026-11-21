# Plugins isolés : le compagnon `plugin-runner`

Un plugin tiers qui tourne **dans le processus de Shelfhold** a les mêmes droits que lui
([plugins-tiers.md](plugins-tiers.md)). Le compagnon `plugin-runner` change cela : le plugin tourne
dans **un autre conteneur**, sous **un autre uid**, **sans réseau**, **sans `/data`**, **sans le
socket de la ligne de commande**, avec une racine en lecture seule. C'est Docker qui coupe ; le
compagnon **vérifie** au démarrage que la coupure est faite, et refuse de démarrer sinon (EF-SRC-016,
ENF-SEC-009, EF-USR-024).

Cette page est une **action de votre côté** : le compagnon n'est dans aucun compose actif par défaut.
Sans lui, aucun plugin isolé ne démarre ; l'ancien mode (en processus, derrière
`SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS=1` et une confirmation tapée) reste disponible et inchangé.

## Ce que le compagnon est

- La **même image** que Shelfhold, la commande `shelfhold plugin-runner`, l'utilisateur **1001**
  (le cœur reste `1000` ; aucun des deux n'est root).
- `network_mode: none` : aucune interface hors `lo`, aucune connexion sortante. Un plugin isolé ne
  parle au monde que **par le cœur** : c'est le cœur qui fait les requêtes, avec sa garde
  anti-SSRF, la liste de domaines du manifeste et la rédaction des journaux, et qui injecte
  les secrets de son côté. **Aucun secret ne traverse** : ni message, ni variable d'environnement,
  ni argument.
- Deux montages, **rien d'autre** : `./data/plugins` en **lecture seule** (les roues que le cœur
  installe, avec la garde habituelle : hachage exigé, aucune dépendance, aucun réseau) et
  `./plugin-rpc`, le dossier du socket.
- Un sous-processus **par plugin**, environnement vidé (aucune variable `SHELFHOLD_*`, rien qui
  ressemble à un secret), session propre, `umask 077`, aucun fichier core, mémoire et temps CPU
  bornés. Quand le cœur se déconnecte ou que le plugin s'arrête, son processus est tué.
- Le **cœur se connecte** au socket du compagnon ; le compagnon ne se connecte jamais à rien, et
  rien du cœur (port, base, socket) n'est joignable depuis lui.

## Ce que le compagnon ne fait pas

Tous les plugins isolés d'une instance tournent dans **le même conteneur compagnon**, sous le même uid :
un plugin ne voit rien du cœur, mais il n'est pas cloisonné des autres plugins que vous avez activés
(il peut lire la mémoire d'un autre ou l'arrêter). Aucun secret n'y circule. N'activez que les plugins
dont vous acceptez qu'ils se voient entre eux.

## Ce que le compagnon vérifie, et ce qui l'arrête

Au démarrage, puis à chaque connexion du cœur, il regarde son propre bac à sable. Il **refuse de
démarrer** (code de sortie 3, rien d'ouvert) si :

1. son uid est celui du cœur (lu dans la poignée de main, et confirmé par le noyau pour le pair du
   socket) ou s'il tourne en root ;
2. `/data`, `/store`, `/library` ou `/data/shelfhold.sock` existent **et** sont lisibles ;
3. il a une interface réseau autre que `lo` ;
4. une connexion sortante aboutit ;
5. sa racine n'est pas en lecture seule.

`docker compose logs plugin-runner` dit laquelle est fausse. `shelfhold plugin-runner --check` rejoue
la vérification et s'arrête (0 : prêt).

## Mise en place (compose de référence ou compose simple)

Les deux fichiers portent le service **commenté**, avec ces étapes. Dans l'ordre :

1. Créez les dossiers, avec les bons propriétaires (le compagnon écrit son socket dans le premier ;
   le cœur écrit les roues dans le second) :

   ```sh
   mkdir -p plugin-rpc data/plugins
   sudo chown 1001:1001 plugin-rpc
   sudo chown 1000:1000 data/plugins
   sudo chmod 755 plugin-rpc data/plugins
   ```

2. Décommentez le service `plugin-runner` (même retrait que `shelfhold:`). Ne changez aucun de ses
   réglages de sécurité : `user: "1001:1001"`, `network_mode: none`, `read_only: true`,
   `cap_drop: [ALL]`, `no-new-privileges`. **N'y ajoutez aucun volume** (`/data`, `/store`, la
   bibliothèque, le socket de Docker) : le compagnon refuserait de démarrer, et c'est voulu.
3. Ajoutez au service `shelfhold` la ligne `- ./plugin-rpc:/plugin-rpc` dans `volumes`. Gardez la
   **même étiquette d'image** pour les deux services.
4. `docker compose up -d`, puis `docker compose logs plugin-runner` : le bac à sable est déclaré
   conforme. `docker compose ps` doit montrer le compagnon `healthy`.

Si vous utilisez des volumes nommés plutôt que des dossiers, montez le volume `plugins` à
`/data/plugins` dans le cœur et à `/plugins` (lecture seule) dans le compagnon, et le volume
`plugin-rpc` à `/plugin-rpc` dans les deux ; l'image crée ces dossiers avec les bons propriétaires.

## Ce qui ne marche pas, et pourquoi

- **Le compagnon redémarre en boucle** : lisez son journal. Un `it can see /data` veut dire qu'un
  volume de données est monté chez lui ; `a network interface other than lo` ou `an outbound
  connection is possible`, que `network_mode: none` manque ; `not read-only`, que `read_only: true`
  manque.
- **« refused: uid »** : le cœur et le compagnon ont le même uid, ou le cœur annonce un autre uid
  que celui que voit le noyau. Ne mettez jamais `user: "1000:1000"` au compagnon.
- **Le cœur ne trouve pas le compagnon** : le dossier `plugin-rpc` n'est pas le même des deux côtés,
  ou n'appartient pas à `1001`. Le cœur ne crée jamais le socket : il se connecte, c'est tout.
- **Un plugin refuse de démarrer** : son module doit être installé dans `data/plugins/site-packages`
  par `shelfhold plugin install`. Le compagnon ne démarre que ce qu'il y trouve, par son nom, jamais
  par un chemin que le cœur lui donne.

## Pour l'auteur d'un plugin

Le kit de conformité (`shelfhold_sdk.testing`) joue, en **mode isolé** (`isolated=True`), le contrôle
« le sous-processus ne voit ni `/data` ni le socket » : aucun des quatre chemins n'est joignable,
l'environnement ne contient aucune variable `SHELFHOLD_*` ni de nom évoquant un secret, l'uid n'est
pas celui du cœur. Voir [../plugins/kit.md](../plugins/kit.md).
