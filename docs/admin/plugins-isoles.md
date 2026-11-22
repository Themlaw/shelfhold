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
  bornés. Quand le cœur se déconnecte ou que le plugin s'arrête, son processus est tué, **avec tout
  ce qu'il avait lancé**, même un processus qui a quitté sa session (le compagnon adopte les
  orphelins de son conteneur et les tue).
- Les plugins tournent sous **l'uid du compagnon** : l'uid ne les sépare donc pas du compagnon.
  Ce qui les en sépare, c'est **Landlock**, appliqué par le compagnon au processus du plugin avant
  de le lancer, sans privilège : le plugin **n'écrit nulle part** (ni création, ni suppression, ni
  renommage, ni lien, ni socket) sauf dans **son dossier de travail** (`$TMPDIR`, un dossier du
  `tmpfs` `/tmp` du compagnon, supprimé à la fin) et `/dev/null`. Il ne peut donc ni remplacer le
  socket du compagnon, ni écrire dans `/plugin-rpc` ni dans `/plugins`. Avec un noyau qui offre
  l'ABI 4 de Landlock (6.7 ou plus), il ne peut pas non plus ouvrir de connexion TCP : une seconde
  barrière réseau derrière `network_mode: none`.
- Un **filtre seccomp**, posé avec Landlock avant le lancement du plugin (construit à la main, sans
  dépendance, pour **x86_64 et aarch64** ; sur une autre architecture le compagnon ne démarre pas) :
  les changements de mode, de propriétaire et d'attributs (`chmod`, `fchmod`, `fchmodat`,
  `fchmodat2`, `chown` et sa famille, `setxattr`, `removexattr` et leurs familles, `io_uring`, qui a son propre `SETXATTR`) échouent avec `EPERM`, et `setsid`
  et `setpgid` **ne font rien** (ils rendent 0) : le plugin reste dans son groupe de processus, que
  le compagnon tue d'un coup à la fin de la connexion, même pendant qu'il lance des fils. Sans ce
  filtre, un plugin, propriétaire de `/plugin-rpc` (l'uid du compagnon), pourrait rendre le socket
  inaccessible au cœur avec `chmod`, ou fabriquer un processus qui change de pid toutes les
  quelques millisecondes. Un plugin qui copie des fichiers avec `shutil.copy` (qui copie le mode)
  échoue donc dans son dossier de travail ; `shutil.copyfile` convient.
- Le compagnon récupère **en continu** les processus que ses plugins ont abandonnés (il adopte les
  orphelins de son conteneur), au lieu d'attendre la fin d'une connexion : un plugin qui
  « fork-bombe » ne peut pas épuiser les 128 processus du conteneur par ses zombies. Le balayage de
  fin de connexion reste en filet : il arrête les processus adoptés avant de les tuer.
- Le compagnon n'est **pas « dumpable »** : un plugin ne lit ni son environnement ni sa mémoire.
- Il **ne sert qu'un uid**, celui du cœur (`--core-uid`, 1000 par défaut), lu par le noyau sur le
  socket : un autre utilisateur de la machine qui atteindrait le socket est refusé. Côté cœur, la
  connexion est refusée si `runner.sock` n'est pas un socket (un lien symbolique n'est jamais suivi).
- Le **cœur se connecte** au socket du compagnon ; le compagnon ne se connecte jamais à rien, et
  rien du cœur (port, base, socket) n'est joignable depuis lui.

## Ce que le compagnon ne fait pas

Tous les plugins isolés d'une instance tournent dans **le même conteneur compagnon**, sous le même uid :
un plugin ne voit rien du cœur et ne peut ni écrire dans le compagnon ni se faire passer pour lui auprès
du cœur, mais il n'est pas cloisonné des **autres plugins** que vous avez activés (il peut les arrêter ou
lire leur mémoire avec `ptrace`, que le profil par défaut de Docker permet). Aucun secret du cœur
(clé, jeton, mot de passe) n'y circule ; ce qu'un plugin voit d'un autre est ce que le cœur lui envoie.
N'activez que les plugins dont vous acceptez qu'ils se voient entre eux ; un compagnon par plugin
serait l'étape suivante.

## Ce que le compagnon vérifie, et ce qui l'arrête

Au démarrage, puis à chaque connexion du cœur, il regarde son propre bac à sable. Il **refuse de
démarrer** (code de sortie 3, rien d'ouvert) si :

1. son uid est celui du cœur (lu dans la poignée de main, et confirmé par le noyau pour le pair du
   socket) ou s'il tourne en root ;
2. `/data`, `/store`, `/library` ou `/data/shelfhold.sock` existent **et** sont lisibles ;
3. il a une interface réseau autre que `lo` ;
4. une connexion sortante aboutit ;
5. sa racine n'est pas en lecture seule ;
6. le noyau n'offre pas **Landlock** (5.13 ou plus, `landlock` dans `cat /sys/kernel/security/lsm`) :
   sans lui, un plugin pourrait remplacer le socket du compagnon, donc il ne démarre pas ;
7. `/var/run/docker.sock` ou `/run/docker.sock` est visible ;
8. le filtre seccomp ne peut pas être posé (architecture inconnue, ou noyau/profil qui l'interdit) ;
9. `/plugins` contient autre chose que `site-packages` et l'état (`state.json`) : c'est le signe
   d'un montage trop large, `./data:/plugins` par exemple.

Le **contrôle de santé** du conteneur (`shelfhold plugin-runner --healthcheck`) vérifie aussi que le
cœur peut entrer : le socket est un socket, lisible et inscriptible par les autres, dans un dossier
traversable par les autres, et il accepte. Le compagnon rétablit le mode `0666` de son socket s'il
a changé, et le dit dans son journal (`plugin_runner.mode_restored`) ; le mode du **dossier**, qui est
celui du volume, est signalé (`plugin_runner.mode_drift`) mais jamais modifié : le conteneur est
alors `unhealthy` et le cœur ne peut plus entrer, ce qui se voit. À chaque connexion, le compagnon
revérifie le socket et refuse (avec la raison) si le cœur ne peut pas compter dessus.

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
   `cap_drop: [ALL]`, `no-new-privileges`, et son `tmpfs: ["/tmp:size=64m"]` (le dossier de travail
   de chaque plugin ; sans lui, aucun plugin ne démarre sur une racine en lecture seule). **N'y ajoutez aucun volume** (`/data`, `/store`, la
   bibliothèque, le socket de Docker) : le compagnon refuserait de démarrer, et c'est voulu.
3. Ajoutez au service `shelfhold` la ligne `- ./plugin-rpc:/plugin-rpc` dans `volumes`. Gardez la
   **même étiquette d'image** pour les deux services.
4. **Si votre dossier `data` ou votre volume nommé existait déjà** (créé par une version antérieure),
   `/data` et `/store` gardent le mode qu'ils avaient : l'image ne règle `0700` que sur un volume
   neuf. Le compagnon (même image, autre uid) les verrait alors, et refuserait de démarrer. Une fois,
   en tant que cœur : `docker compose exec shelfhold chmod 700 /data /store`.
5. `docker compose up -d`, puis `docker compose logs plugin-runner` : le bac à sable est déclaré
   conforme. `docker compose ps` doit montrer le compagnon `healthy`.

Si vous utilisez des volumes nommés plutôt que des dossiers, montez le volume `plugins` à
`/data/plugins` dans le cœur et à `/plugins` (lecture seule) dans le compagnon, et le volume
`plugin-rpc` à `/plugin-rpc` dans les deux ; l'image crée ces dossiers avec les bons propriétaires.

## Ce qui ne marche pas, et pourquoi

- **Le compagnon redémarre en boucle** : lisez son journal. Un `it can see /data` veut dire qu'un
  volume de données est monté chez lui ; `a network interface other than lo` ou `an outbound
  connection is possible`, que `network_mode: none` manque ; `not read-only`, que `read_only: true`
  manque ; `the kernel offers no Landlock`, que l'hôte n'a pas Landlock (noyau 5.13 ou plus avec
  `landlock` dans les LSM actifs) ; `the plugins volume holds more than the installed plugins`, que
  `/plugins` est un montage trop large. `the seccomp filter ... cannot be installed here`, que l'architecture n'est ni
  x86_64 ni aarch64 ou que le profil seccomp de Docker interdit `seccomp(2)` ou `prctl` sur ce
  conteneur. Docker doit aussi laisser passer les appels système de Landlock : c'est le cas du profil seccomp par défaut des versions récentes de Docker ; un profil
  personnalisé qui les interdit empêche le compagnon de démarrer.
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
pas celui du cœur. Un plugin isolé n'écrit que dans `$TMPDIR` : tout autre fichier qu'il crée est
refusé par le noyau (`PermissionError`). Voir [../plugins/kit.md](../plugins/kit.md).
