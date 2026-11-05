# Configuration

Précédence (ENF-OPS-004) : variable d'environnement, puis `config.toml` (dans `/data`), puis
base de données, puis valeur par défaut. Un champ défini par l'environnement est en lecture seule
(`shelfhold config show`). Chaque variable accepte un suffixe `_FILE` qui en lit la valeur dans un
fichier (secrets Docker) ; limitez les droits du fichier (`chmod 600`), un avertissement est émis
s'il est lisible par tous.

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `SHELFHOLD_DATA_DIR` | `/data` | Volume chaud : SQLite (WAL), `config.toml`, sauvegardes de migration. Variable seule (jamais dans `config.toml`) |
| `SHELFHOLD_STORE_DIR` | `/store` | Volume des copies conservées ; distinct de `/data` et non imbriqué (ENF-OPS-012) |
| `SHELFHOLD_FRONTEND_DIR` | (build embarqué) | Répertoire du build statique de la SPA servi par le backend ; sans build, seule l'API répond |
| `SHELFHOLD_HOST` | `127.0.0.1` | Adresse d'écoute. **Dans un conteneur, définir `SHELFHOLD_HOST=0.0.0.0`** (le Dockerfile arrive en PR5) |
| `SHELFHOLD_PORT` | `8080` | Port d'écoute |
| `SHELFHOLD_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` ou `ERROR` |
| `SHELFHOLD_LOG_FORMAT` | `json` | `json` (conteneur) ou `console` (développement) |
| `SHELFHOLD_PUBLIC_URL` | aucune | URL publique de l'instance |
| `SHELFHOLD_API_TOKEN` | aucun | Jeton d'API réservé à l'interface headless (M1a), secret : masqué partout |
| `SHELFHOLD_SECRET_KEY_FILE` | `/data/keyring` | Chemin du trousseau des secrets (JSON, 0600). Absent : créé dans `/data` au premier démarrage. **Recommandé hors de `/data`** (secret Docker, volume distinct) ; `shelfhold secrets status` avertit tant qu'il est dans `/data`. Fichier configuré mais absent, ou base qui contient déjà des secrets sans trousseau : démarrage refusé (jamais de nouvelle clé qui rendrait les secrets illisibles). Variable seule. Fichier fourni par l'hôte (secret Docker ou Kubernetes) : lien symbolique, lecture seule et propriétaire root acceptés, fichier modifiable par le groupe ou les autres refusé ; un fichier lisible par tous donne un avertissement dans `secrets status` (durcissement : le limiter à l'utilisateur de Shelfhold). Le trousseau de `/data` est tenu à 0600, même uid, sans lien symbolique |
| `SHELFHOLD_SOURCE_URL` | dépôt au tag de la version | Lien « Code source » de la page de connexion et `source_url` de `GET /api/v1/about` (AGPL §13, ENF-LEG-012). Si vous avez modifié le code, pointez-le vers votre version. Le lien par défaut révèle la version qui tourne |
| `SHELFHOLD_AUTH_HARDENED` | `false` | Profil durci (`docs/admin/durcir.md`) : session admin de 30 min d'inactivité et 12 h au plus, mot de passe redemandé pour toute action réservée, clés d'API plafonnées à 90 jours. Jamais exigé |
| `SHELFHOLD_FRESH_AUTH_S` | `900` | Fenêtre (secondes, 60 à 86 400) pendant laquelle une session peut faire une action irréversible ou qui révèle un secret sans redemander le mot de passe (EF-USR-022) |
| `SHELFHOLD_CLI_ALLOW_HOST` | `false` | Accepte sur la socket de la CLI locale (`/data/shelfhold.sock`) un processus hors de l'espace de PID du conteneur (l'hôte, un autre conteneur qui monte `/data`). Refusé par défaut et journalisé ; activé, l'utilisateur de Shelfhold sur l'hôte vaut un administrateur (EF-USR-024) |
| `SHELFHOLD_NAMING_SEGMENT_POLICY_CONFIRMED` | `false` | Q50 : tant qu'il est faux, une Unité dont la place porte un segment de numérotation non vide reste en `numbering_conflict` et n'est pas exportée. À ne passer à vrai qu'après la confirmation du nommage « une série Kavita par segment » par le test nocturne contre un vrai Kavita (`test_segment_naming_against_real_kavita_nightly`, L40) |
| `SHELFHOLD_FORWARD_AUTH_TRUSTED_CIDRS` | aucun | Réseaux du proxy de forward-auth, séparés par des virgules. Vide : forward-auth désactivé, et tout en-tête d'identité (`Remote-User`, `Tailscale-User-*`…) est retiré des requêtes sans être cru. Configuré : ces en-têtes ne sont conservés que pour les requêtes venues de ces réseaux (leur lecture arrive en M3) |
| `SHELFHOLD_ALLOW_NETWORK_DATA_DIR` | `false` | `/data` sur NFS, CIFS/SMB, 9p, Ceph, AFS ou `fuse.sshfs` est refusé au démarrage (détection `statfs`, `/proc/self/mountinfo` pour FUSE) : SQLite et le `flock` d'instance exigent un disque local (ENF-OPS-010). À `true`, le démarrage continue avec un avertissement, repris par `shelfhold status`, à vos risques (base corrompue, deux instances). `/store` et `/library` peuvent être en réseau (leur sentinelle les garde). Si `statfs` échoue, un avertissement est journalisé et rien n'est refusé |
| `SHELFHOLD_DISK_PAUSE_PERCENT` / `SHELFHOLD_DISK_PAUSE_GIB` | `5` / `10` | Garde disque (ENF-OPS-006) : sous `max(pourcentage, Gio)` libres sur `/data`, `/store` ou une racine de la bibliothèque, les files d'écriture sont mises en pause (`disk.low`, aucune suppression) |
| `SHELFHOLD_DISK_RESUME_PERCENT` / `SHELFHOLD_DISK_RESUME_GIB` | `8` / `15` | Reprise au-dessus de `max(pourcentage, Gio)` ; doivent être strictement supérieurs aux seuils de pause |
