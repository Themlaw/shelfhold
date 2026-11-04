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
| `SHELFHOLD_HOST` | `127.0.0.1` | Adresse d'écoute. **Dans un conteneur, définir `SHELFHOLD_HOST=0.0.0.0`** (le Dockerfile arrive en PR5) |
| `SHELFHOLD_PORT` | `8080` | Port d'écoute |
| `SHELFHOLD_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` ou `ERROR` |
| `SHELFHOLD_LOG_FORMAT` | `json` | `json` (conteneur) ou `console` (développement) |
| `SHELFHOLD_PUBLIC_URL` | aucune | URL publique de l'instance |
| `SHELFHOLD_API_TOKEN` | aucun | Jeton d'API réservé à l'interface headless (M1a), secret : masqué partout |
