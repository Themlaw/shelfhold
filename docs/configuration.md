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
