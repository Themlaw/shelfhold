# Mode isolé recommandé (M6)

Un plugin tiers **isolé** tourne dans le conteneur compagnon `plugin-runner` : un autre
processus, un autre uid, sans réseau, sans `/data`, sans le socket de la ligne de commande,
racine en lecture seule. C'est Docker qui coupe ; le compagnon **vérifie** au démarrage que la
coupure est faite et refuse de démarrer sinon (EF-SRC-016, ENF-SEC-009, EF-USR-024).

Le mode isolé est le **défaut** et la **recommandation**. Le mode en processus (`in_process`)
reste disponible, derrière une variable d'environnement et une confirmation tapée, pour les
plugins que l'administrateur accepte de voir courir dans le processus de Shelfhold.

## Ce que le sous-processus ne voit pas

Au démarrage, puis à chaque connexion du cœur, le compagnon vérifie son bac à sable. Il refuse
de démarrer (code 3, rien d'ouvert) si :

- son uid est celui du cœur ou s'il tourne en root ;
- `/data`, `/store`, `/library` ou `/data/shelfhold.sock` existent **et** sont lisibles ;
- il a une interface réseau autre que `lo` ;
- une connexion sortante aboutit ;
- sa racine n'est pas en lecture seule ;
- le noyau n'offre pas Landlock ABI 3 ou plus (noyau 6.2 ou plus) ;
- `/var/run/docker.sock` ou `/run/docker.sock` est visible ;
- le filtre seccomp ne peut pas être posé (x86_64 ou aarch64 seulement) ;
- `/plugins` contient autre chose que `site-packages`, `state.json`, `index`, `wheels` et `previous`.

## Ce que le kit contrôle

Avec `isolated=True` (`run_source_conformance`, `run_metadata_conformance`,
`run_destination_conformance`, `assert_conforms`, la fixture `check_conformance`) :

| Contrôle | Ce qu'il vérifie |
| --- | --- |
| `isolation_no_data` | ni `/data`, ni `/store`, ni `/library` ne sont joignables |
| `isolation_no_cli_socket` | `/data/shelfhold.sock` n'est pas joignable |
| `isolation_env_closed` | aucune variable `SHELFHOLD_*` ni de nom qui évoque un secret |
| `isolation_uid_distinct` | l'uid n'est ni celui du cœur ni root |

`sandbox=SandboxView(...)` remplace l'observation du processus pour un test qui décrit un bac à
sable sans conteneur ; `python -m shelfhold_sdk.testing.isolation` l'imprime en JSON.

## Ce que le plugin peut faire

- parler au monde **par le cœur** : le cœur fait les requêtes, avec sa garde anti-SSRF, la liste
  de domaines du manifeste et la rédaction des journaux ; il injecte les secrets de son côté ;
- écrire seulement dans son dossier de travail (`$TMPDIR`, un `tmpfs` supprimé à la fin) ;
- lire ses propres sources installées.

Aucun secret ne traverse : ni message, ni variable d'environnement, ni argument.

## Ce que le plugin ne peut pas faire

- écrire ailleurs que dans `$TMPDIR` (Landlock : `PermissionError` ailleurs) ;
- ouvrir une connexion TCP avec un noyau offrant Landlock ABI 4 (6.7+) ;
- changer de mode, de propriétaire ou d'attribut (`chmod`, `chown`, `setxattr`… : `EPERM`) ;
- quitter son groupe de processus (`setsid`, `setpgid` ne font rien) ;
- lire l'environnement ou la mémoire du compagnon (non « dumpable »).

## Noyaux sans portée des signaux (Landlock ABI 3 à 5)

Avec un noyau 6.12 ou plus (Landlock ABI 6), Landlock borne les signaux. Avant, le compagnon
s'appuie sur un filtre seccomp, qui laisse un plugin signaler **lui-même et son groupe de
processus** seulement. Conséquences pour un auteur :

- pour arrêter un sous-processus, signalez le **groupe** : `os.killpg(os.getpgid(0), sig)` (le
  plugin est le chef de son groupe, ses enfants en font partie) ;
- `subprocess.run(timeout=…)` lève `PermissionError` à l'échéance au lieu de `TimeoutExpired`, et
  laisse le fils vivant ; `Popen.terminate()` et `Popen.kill()` lèvent `PermissionError` ;
- un processus forké ne peut pas appeler `signal.raise_signal` sur lui-même par son pid ;
- `fcntl(F_SETOWN, …)` et `ioctl(FIOSETOWN, …)` échouent avec `EPERM`.

Le compagnon tue de toute façon tout le groupe à la fin de la connexion.

Les plugins d'un même compagnon ne sont pas cloisonnés les uns des autres : n'activez que des
plugins dont vous acceptez qu'ils se voient.

## Ce que le manifeste ne peut pas faire

Le manifeste d'un plugin **ne peut pas déclarer son mode** : toute clé `mode` est refusée par
l'hôte. C'est le foyer qui décide, jamais le code du plugin (EF-SRC-019).

## Licence

Un plugin isolé relève de la même exception de plugin provisoire (ENF-LEG-010) : tant qu'elle
n'est pas relue, préférez une licence compatible AGPL.
