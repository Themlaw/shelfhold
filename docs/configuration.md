# Configuration

La liste complète, avec types et bornes, est générée du code : [`docs/reference/config.md`](reference/config.md).

Précédence (ENF-OPS-004) : variable d'environnement, puis `config.toml` (dans `/data`), puis
base de données, puis valeur par défaut. Un champ défini par l'environnement est en lecture seule
(`shelfhold config show`). Chaque variable accepte un suffixe `_FILE` qui en lit la valeur dans un
fichier (secrets Docker) ; limitez les droits du fichier (`chmod 600`), un avertissement est émis
s'il est lisible par tous.

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `SHELFHOLD_DATA_DIR` | `/data` | Volume chaud : SQLite (WAL), `config.toml`, sauvegardes de migration. Variable seule (jamais dans `config.toml`) |
| `SHELFHOLD_STORE_DIR` | `/store` | Volume des copies conservées ; distinct de `/data` et non imbriqué (ENF-OPS-012) |
| `SHELFHOLD_LIBRARY_DIR` | `/library` | Dossier sous lequel vivent les racines physiques en écriture (EF-EXP-059) ; ni `/data` ni `/store` |
| `SHELFHOLD_FRONTEND_DIR` | (build embarqué) | Répertoire du build statique de la SPA servi par le backend ; sans build, seule l'API répond |
| `SHELFHOLD_HOST` | `127.0.0.1` | Adresse d'écoute. **Dans un conteneur, définir `SHELFHOLD_HOST=0.0.0.0`** (le Dockerfile arrive en PR5) |
| `SHELFHOLD_PORT` | `8080` | Port d'écoute |
| `SHELFHOLD_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` ou `ERROR` |
| `SHELFHOLD_LOG_FORMAT` | `json` | `json` (conteneur) ou `console` (développement) |
| `SHELFHOLD_UMASK` | `0022` | Umask (octal) des fichiers et dossiers que Shelfhold crée sous la bibliothèque (EF-EXP-006) : `0022` donne 0644/0755, lisibles par un Kavita d'un autre uid ; `0002` ajoute l'écriture au groupe (avec `group_add`). Le processus reste sous `077` pour `/data`, et les secrets sont en 0600 par un mode explicite, quel que soit ce réglage |
| `SHELFHOLD_PUBLIC_URL` | aucune | URL publique de l'instance |
| `SHELFHOLD_API_TOKEN` | aucun | Jeton d'API réservé à l'interface headless (M1a), secret : masqué partout |
| `SHELFHOLD_SECRET_KEY_FILE` | `/data/keyring` | Chemin du trousseau des secrets (JSON, 0600). Absent : créé dans `/data` au premier démarrage. **Recommandé hors de `/data`** (secret Docker, volume distinct) ; `shelfhold status` et `shelfhold secrets status` avertissent tant qu'il est dans `/data`. Fichier configuré mais absent, ou base qui contient déjà des secrets sans trousseau : démarrage refusé (jamais de nouvelle clé qui rendrait les secrets illisibles). Variable seule. Fichier fourni par l'hôte (secret Docker ou Kubernetes) : lien symbolique, lecture seule et propriétaire root acceptés, fichier modifiable par le groupe ou les autres refusé ; un fichier lisible par tous donne un avertissement dans `secrets status` (durcissement : le limiter à l'utilisateur de Shelfhold). Le trousseau de `/data` est tenu à 0600, même uid, sans lien symbolique |
| `SHELFHOLD_SOURCE_URL` | dépôt au tag de la version | Lien « Code source » de la page de connexion et `source_url` de `GET /api/v1/about` (AGPL §13, ENF-LEG-012). Si vous avez modifié le code, pointez-le vers votre version. Le lien par défaut révèle la version qui tourne |
| `SHELFHOLD_RECEIVED_CBZ_MAX_BYTES` | `536870912` (512 Mio) | Plafond d'une archive CBZ reçue d'un service connecté (au moins 1 Mio), contrôlé pendant la réception (ENF-SEC-002). Un fichier plus gros est refusé avant d'être ouvert |
| `SHELFHOLD_AUTH_HARDENED` | `false` | Profil durci (`docs/admin/durcir.md`) : session admin de 30 min d'inactivité et 12 h au plus, mot de passe redemandé pour toute action réservée, clés d'API plafonnées à 90 jours. Jamais exigé |
| `SHELFHOLD_FRESH_AUTH_S` | `900` | Fenêtre (secondes, 60 à 86 400) pendant laquelle une session peut faire une action irréversible ou qui révèle un secret sans redemander le mot de passe (EF-USR-022) |
| `SHELFHOLD_CLI_ALLOW_HOST` | `false` | Accepte sur la socket de la CLI locale (`/data/shelfhold.sock`) un processus hors de l'espace de PID du conteneur (l'hôte, un autre conteneur qui monte `/data`). Refusé par défaut et journalisé ; activé, l'utilisateur de Shelfhold sur l'hôte vaut un administrateur (EF-USR-024) |
| `SHELFHOLD_NAMING_SEGMENT_POLICY_CONFIRMED` | `false` | Q50 : tant qu'il est faux, une Unité dont la place porte un segment de numérotation non vide reste en `numbering_conflict` et n'est pas exportée. À ne passer à vrai qu'après la confirmation du nommage « une série Kavita par segment » par le test nocturne contre un vrai Kavita (`test_segment_naming_against_real_kavita_nightly`, L40) |
| `SHELFHOLD_HTTP_TRUSTED_PROXIES` | aucun | Réseaux (CIDR, `/32` conseillé) des proxys inverses qui passent devant Shelfhold, séparés par des virgules. Seul un appelant dont l'adresse de socket est dans l'un d'eux est cru quand il écrit `X-Forwarded-For` : l'adresse réelle d'un visiteur (limitation de débit) est la première de la chaîne, lue **de droite à gauche**, qui n'est pas elle-même un proxy de confiance. Vide : l'adresse est toujours celle de la socket. Un réseau `/0` est refusé |
| `SHELFHOLD_AUTH_FORWARD_ENABLED` | `false` | Forward-auth (EF-USR-002), **désactivé par défaut**. Activé, il exige `SHELFHOLD_HTTP_TRUSTED_PROXIES` (démarrage refusé sans) et, dès que ce réseau est plus large qu'un hôte, `SHELFHOLD_AUTH_FORWARD_SECRET`. Le nom donné par `Remote-User` n'est cru que d'un proxy de confiance, jamais pour `/opds`, `/kosync`, `/kobo` (refus 403 journalisé), et ne donne jamais accès à une action réservée sans ré-authentification par mot de passe local. Désactivé, tout en-tête d'identité (`Remote-User`, `Tailscale-User-*`…) est retiré de la requête sans être cru ni refusé |
| `SHELFHOLD_AUTH_FORWARD_SECRET` | aucun | Secret partagé avec le proxy, attendu dans `X-Shelfhold-Proxy-Secret` (comparaison à temps constant). Obligatoire si le réseau du proxy est plus large qu'un hôte ; facultatif pour un `/32` ou un `/128`, où l'adresse est alors la seule garantie. Aussi lisible depuis un fichier (`…_FILE`) |
| `SHELFHOLD_HTTP_PUBLIC_RATE_PER_MINUTE` | `120` | Requêtes par minute et par adresse réelle pour un appelant non connecté (page de connexion, connexion…). Au-delà : 429 avec `Retry-After`. Les sondes `/healthz` et `/readyz` n'y sont pas soumises |
| `SHELFHOLD_HTTP_DEVICE_RATE_PER_MINUTE` | `600` | Requêtes par minute et **par identifiant** (jeton d'appareil, Basic) sur `/opds`, `/kosync` et `/kobo`. Sans identifiant, la requête compte comme celle d'un visiteur anonyme |
| `SHELFHOLD_ALLOW_NETWORK_DATA_DIR` | `false` | `/data` sur NFS, CIFS/SMB, 9p, Ceph, AFS ou `fuse.sshfs` est refusé au démarrage (détection `statfs`, `/proc/self/mountinfo` pour FUSE) : SQLite et le `flock` d'instance exigent un disque local (ENF-OPS-010). À `true`, le démarrage continue avec un avertissement, repris par `shelfhold status`, à vos risques (base corrompue, deux instances). `/store` et `/library` peuvent être en réseau (leur sentinelle les garde). Si `statfs` échoue, un avertissement est journalisé et rien n'est refusé |
| `SHELFHOLD_DISK_PAUSE_PERCENT` / `SHELFHOLD_DISK_PAUSE_GIB` | `5` / `10` | Garde disque (ENF-OPS-006) : sous `max(pourcentage, Gio)` libres sur `/data`, `/store` ou une racine de la bibliothèque, les files d'écriture sont mises en pause (`disk.low`, aucune suppression) |
| `SHELFHOLD_DISK_RESUME_PERCENT` / `SHELFHOLD_DISK_RESUME_GIB` | `8` / `15` | Reprise au-dessus de `max(pourcentage, Gio)` ; doivent être strictement supérieurs aux seuils de pause |
| `SHELFHOLD_BACKUP_DIR` | aucun | Dossier de la sauvegarde intégrée, hors de `/data` et de `/store` ; sans lui, aucune sauvegarde (`docs/admin/sauvegarde.md`) |
| `SHELFHOLD_BACKUP_PASSPHRASE_FILE` | aucun | Fichier (droits 0600, 12 caractères au moins) qui contient la phrase de passe des sauvegardes ; variable seule |
| `SHELFHOLD_BACKUP_RETENTION` / `SHELFHOLD_BACKUP_STALE_HOURS` | `14` / `36` | Nombre de sauvegardes gardées (1 à 30) ; âge au-delà duquel `backup.stale` et un avertissement de `status` apparaissent |
| `SHELFHOLD_WAKE_URL`, `_WAKE_SUWAYOMI`, `_WAKE_KAVITA`, `_WAKE_TIMEOUT_S`, `_WAKE_SESSION_S`, `_WAKE_TOKEN` | aucun (60 s, 300 s) | Réveil à la demande par Sablier (`docs/admin/reveil-sablier.md`) |
| `SHELFHOLD_MAINTENANCE_WINDOW_START` / `_END` / `SHELFHOLD_TIMEZONE` | `03:00` / `07:00` / `Europe/Paris` | Fenêtre de maintenance ; elle peut traverser minuit, et la changer replanifie les tâches de fenêtre |
| `SHELFHOLD_RETENTION_EVENTS_DAYS` / `SHELFHOLD_RETENTION_AUDIT_DAYS` | `90` / `365` | Rétention des événements et tâches terminés (7 à 3650 jours), de l'audit (90 à 3650) ; la corbeille garde 30 jours, non réglable |

## Valeurs par défaut sans réglage (M1a)

Ces valeurs ne se règlent pas encore ; `shelfhold status` les applique et les rappelle ici.

| Valeur | Défaut | Rôle |
| --- | --- | --- |
| Fraîcheur d'une série (`k`) | `3` | Une série suivie est « silencieuse » (`series.stale`) quand aucun chapitre n'est découvert depuis `k` fois sa cadence observée (EF-SRC-026 c) |
| Plancher de fraîcheur | 14 jours | Seuil d'une série sans cadence mesurable (moins de deux écarts entre découvertes distinctes) |
| Âge de la liste `known_bad_versions` | alerte après 120 jours | La liste des versions de Kavita connues pour une faille est livrée avec la version de Shelfhold ; `status` affiche son âge (ENF-COMP-001). Version minimale de Kavita : 0.9.1 |
