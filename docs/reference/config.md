# Référence de la configuration

<!-- Fichier généré : `python -m shelfhold.docgen.config_reference`.
     Ne pas modifier à la main : un test échoue si ce fichier n'est pas à jour. -->

Version du schéma de `config.toml` : `1.0`. Précédence : variable d'environnement, puis `config.toml`, puis base de données, puis valeur par défaut. Le texte d'accompagnement (rôle de chaque variable, exemples) est dans [`docs/configuration.md`](../configuration.md), les commandes dans [`docs/admin/headless.md`](../admin/headless.md).

## Variables d'environnement et `config.toml`

| Variable | Clé de `config.toml` | Type | Défaut | Bornes | Remarque |
| --- | --- | --- | --- | --- | --- |
| `SHELFHOLD_DATA_DIR` | `data_dir` | Path | `/data` |  | variable seule, jamais dans `config.toml`; `SHELFHOLD_DATA_DIR_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_STORE_DIR` | `store_dir` | Path | `/store` |  | `SHELFHOLD_STORE_DIR_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_LIBRARY_DIR` | `library_dir` | Path | `/library` |  | `SHELFHOLD_LIBRARY_DIR_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_IMPORT_DIR` | `import_dir` | Path (optionnel) | aucun |  | `SHELFHOLD_IMPORT_DIR_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_HOST` | `host` | str | `127.0.0.1` |  | `SHELFHOLD_HOST_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_PORT` | `port` | int | `8080` | >= 1, <= 65535 | `SHELFHOLD_PORT_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_LOG_LEVEL` | `log_level` | 'DEBUG' \| 'INFO' \| 'WARNING' \| 'ERROR' | `INFO` |  | `SHELFHOLD_LOG_LEVEL_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_LOG_FORMAT` | `log_format` | 'json' \| 'console' | `json` |  | `SHELFHOLD_LOG_FORMAT_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_UMASK` | `umask` | int | `0022` | >= 0, <= 511 | `SHELFHOLD_UMASK_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_PUBLIC_URL` | `public_url` | str (optionnel) | aucun |  | `SHELFHOLD_PUBLIC_URL_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_CONTENT_ORIGIN` | `content_origin` | str (optionnel) | aucun |  | `SHELFHOLD_CONTENT_ORIGIN_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_API_TOKEN` | `api_token` | secret (optionnel) | aucun |  | secret, masqué partout; `SHELFHOLD_API_TOKEN_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_SECRET_KEY_FILE` | `secret_key_file` | Path (optionnel) | aucun |  | variable seule, jamais dans `config.toml` |
| `SHELFHOLD_FRONTEND_DIR` | `frontend_dir` | Path (optionnel) | aucun |  | `SHELFHOLD_FRONTEND_DIR_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_PLUGIN_REPOS` | `plugin_repos` | liste de str | aucun |  | `SHELFHOLD_PLUGIN_REPOS_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_ACTIVE_SOURCES` | `active_sources` | liste de str | aucun |  | `SHELFHOLD_ACTIVE_SOURCES_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_SOURCE_URL` | `source_url` | str (optionnel) | aucun |  | `SHELFHOLD_SOURCE_URL_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_UPDATE_CHECK_URL` | `update_check_url` | str (optionnel) | aucun |  | `SHELFHOLD_UPDATE_CHECK_URL_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_PLUGIN_INDEX_URL` | `plugin_index_url` | str (optionnel) | aucun |  | `SHELFHOLD_PLUGIN_INDEX_URL_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_RECEIVED_CBZ_MAX_BYTES` | `received_cbz_max_bytes` | int | `536870912` | >= 1048576 | `SHELFHOLD_RECEIVED_CBZ_MAX_BYTES_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_AUTH_HARDENED` | `auth_hardened` | bool | `false` |  | `SHELFHOLD_AUTH_HARDENED_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_OIDC_ISSUER` | `oidc_issuer` | str (optionnel) | aucun |  | `SHELFHOLD_OIDC_ISSUER_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_OIDC_CLIENT_ID` | `oidc_client_id` | str (optionnel) | aucun |  | `SHELFHOLD_OIDC_CLIENT_ID_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_OIDC_CLIENT_SECRET` | `oidc_client_secret` | secret (optionnel) | aucun |  | secret, masqué partout; `SHELFHOLD_OIDC_CLIENT_SECRET_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_OIDC_MAX_AGE_S` | `oidc_max_age_s` | int | `300` | >= 30, <= 900 | `SHELFHOLD_OIDC_MAX_AGE_S_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_METRICS_ENABLED` | `metrics_enabled` | bool | `false` |  | `SHELFHOLD_METRICS_ENABLED_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_FRESH_AUTH_S` | `fresh_auth_s` | int | `900` | >= 60, <= 86400 | `SHELFHOLD_FRESH_AUTH_S_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_REVISION_MIN_PAGE_RATIO` | `revision_min_page_ratio` | float | `0.5` | >= 0.05, <= 1.0 | `SHELFHOLD_REVISION_MIN_PAGE_RATIO_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_PAGE_TARGET_FORMAT` | `page_target_format` | 'jpeg' \| 'png' \| 'webp' | `jpeg` |  | `SHELFHOLD_PAGE_TARGET_FORMAT_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_PAGE_TARGET_QUALITY` | `page_target_quality` | int | `90` | >= 1, <= 100 | `SHELFHOLD_PAGE_TARGET_QUALITY_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_FOLLOW_FALLBACK_RETURN` | `follow_fallback_return` | bool | `true` |  | `SHELFHOLD_FOLLOW_FALLBACK_RETURN_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_FOLLOW_CHECK_MIN_S` | `follow_check_min_s` | int | `3600` | >= 60 | `SHELFHOLD_FOLLOW_CHECK_MIN_S_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_FOLLOW_CHECK_MAX_S` | `follow_check_max_s` | int | `604800` | >= 60 | `SHELFHOLD_FOLLOW_CHECK_MAX_S_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_FOLLOW_CHECK_FINISHED_S` | `follow_check_finished_s` | int | `2592000` | >= 3600 | `SHELFHOLD_FOLLOW_CHECK_FINISHED_S_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_NAMING_SEGMENT_POLICY_CONFIRMED` | `naming_segment_policy_confirmed` | bool | `false` |  | `SHELFHOLD_NAMING_SEGMENT_POLICY_CONFIRMED_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_HTTP_TRUSTED_PROXIES` | `http_trusted_proxies` | liste de str | aucun |  | `SHELFHOLD_HTTP_TRUSTED_PROXIES_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_AUTH_FORWARD_ENABLED` | `auth_forward_enabled` | bool | `false` |  | `SHELFHOLD_AUTH_FORWARD_ENABLED_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_AUTH_FORWARD_SECRET` | `auth_forward_secret` | secret (optionnel) | aucun |  | secret, masqué partout; `SHELFHOLD_AUTH_FORWARD_SECRET_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_SETUP_TOKEN` | `setup_token` | secret (optionnel) | aucun |  | secret, masqué partout; `SHELFHOLD_SETUP_TOKEN_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_HTTP_PUBLIC_RATE_PER_MINUTE` | `http_public_rate_per_minute` | int | `120` | >= 10, <= 100000 | `SHELFHOLD_HTTP_PUBLIC_RATE_PER_MINUTE_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_HTTP_DEVICE_RATE_PER_MINUTE` | `http_device_rate_per_minute` | int | `600` | >= 10, <= 100000 | `SHELFHOLD_HTTP_DEVICE_RATE_PER_MINUTE_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_DEVICE_ALLOW_PLAIN_HTTP` | `device_allow_plain_http` | bool | `false` |  | `SHELFHOLD_DEVICE_ALLOW_PLAIN_HTTP_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_CLI_ALLOW_HOST` | `cli_allow_host` | bool | `false` |  | `SHELFHOLD_CLI_ALLOW_HOST_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_ALLOW_NETWORK_DATA_DIR` | `allow_network_data_dir` | bool | `false` |  | `SHELFHOLD_ALLOW_NETWORK_DATA_DIR_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_DISK_PAUSE_PERCENT` | `disk_pause_percent` | float | `5.0` | >= 0, <= 50 | `SHELFHOLD_DISK_PAUSE_PERCENT_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_DISK_PAUSE_GIB` | `disk_pause_gib` | float | `10.0` | >= 0 | `SHELFHOLD_DISK_PAUSE_GIB_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_DISK_RESUME_PERCENT` | `disk_resume_percent` | float | `8.0` | >= 0, <= 60 | `SHELFHOLD_DISK_RESUME_PERCENT_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_DISK_RESUME_GIB` | `disk_resume_gib` | float | `15.0` | >= 0 | `SHELFHOLD_DISK_RESUME_GIB_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_READING_RELAY_MAX_SESSION_HOURS` | `reading_relay_max_session_hours` | float | `24.0` | > 0, <= 720 | `SHELFHOLD_READING_RELAY_MAX_SESSION_HOURS_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_WAKE_URL` | `wake_url` | str (optionnel) | aucun |  | `SHELFHOLD_WAKE_URL_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_WAKE_TOKEN` | `wake_token` | secret (optionnel) | aucun |  | secret, masqué partout; `SHELFHOLD_WAKE_TOKEN_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_WAKE_SUWAYOMI` | `wake_suwayomi` | liste de str | aucun |  | `SHELFHOLD_WAKE_SUWAYOMI_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_WAKE_KAVITA` | `wake_kavita` | liste de str | aucun |  | `SHELFHOLD_WAKE_KAVITA_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_WAKE_TIMEOUT_S` | `wake_timeout_s` | int | `60` | >= 5, <= 600 | `SHELFHOLD_WAKE_TIMEOUT_S_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_WAKE_SESSION_S` | `wake_session_s` | int | `300` | >= 30, <= 3600 | `SHELFHOLD_WAKE_SESSION_S_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_MAINTENANCE_WINDOW_START` | `maintenance_window_start` | str | `03:00` |  | `SHELFHOLD_MAINTENANCE_WINDOW_START_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_MAINTENANCE_WINDOW_END` | `maintenance_window_end` | str | `07:00` |  | `SHELFHOLD_MAINTENANCE_WINDOW_END_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_TIMEZONE` | `timezone` | str | `Europe/Paris` |  | `SHELFHOLD_TIMEZONE_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_RETENTION_EVENTS_DAYS` | `retention_events_days` | int | `90` | >= 7, <= 3650 | `SHELFHOLD_RETENTION_EVENTS_DAYS_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_RETENTION_AUDIT_DAYS` | `retention_audit_days` | int | `365` | >= 90, <= 3650 | `SHELFHOLD_RETENTION_AUDIT_DAYS_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_STREAM_MAX_PER_PROFILE` | `stream_max_per_profile` | int | `4` | >= 1, <= 64 | `SHELFHOLD_STREAM_MAX_PER_PROFILE_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_ADOPT_PROBE_WORKERS` | `adopt_probe_workers` | int | `2` | >= 1, <= 8 | `SHELFHOLD_ADOPT_PROBE_WORKERS_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_BACKUP_DIR` | `backup_dir` | Path (optionnel) | aucun |  | `SHELFHOLD_BACKUP_DIR_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_BACKUP_PASSPHRASE_FILE` | `backup_passphrase_file` | Path (optionnel) | aucun |  | variable seule, jamais dans `config.toml` |
| `SHELFHOLD_BACKUP_RETENTION` | `backup_retention` | int | `14` | >= 1, <= 30 | `SHELFHOLD_BACKUP_RETENTION_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_BACKUP_STALE_HOURS` | `backup_stale_hours` | int | `36` | >= 25, <= 1440 | `SHELFHOLD_BACKUP_STALE_HOURS_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_FETCHER_CACHE` | `fetcher_cache` | bool | `true` |  | `SHELFHOLD_FETCHER_CACHE_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_FETCHER_CACHE_OFF_SOURCES` | `fetcher_cache_off_sources` | liste de str | aucun |  | `SHELFHOLD_FETCHER_CACHE_OFF_SOURCES_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_METADATA_CONTACT` | `metadata_contact` | str (optionnel) | aucun |  | `SHELFHOLD_METADATA_CONTACT_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_COMICVINE_ENABLED` | `comicvine_enabled` | bool | `false` |  | `SHELFHOLD_COMICVINE_ENABLED_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_COMICVINE_API_KEY` | `comicvine_api_key` | secret (optionnel) | aucun |  | secret, masqué partout; `SHELFHOLD_COMICVINE_API_KEY_FILE` en lit la valeur dans un fichier |
| `SHELFHOLD_GOOGLEBOOKS_DAILY_BUDGET` | `googlebooks_daily_budget` | int | `1000` | >= 1, <= 1000000 | `SHELFHOLD_GOOGLEBOOKS_DAILY_BUDGET_FILE` en lit la valeur dans un fichier |

## Clés de `config get|set|apply`

Enregistrées en base ; l'environnement et `config.toml` l'emportent. Le rôle est donné tel que le code le décrit.

| Clé | Défaut | Modifiable | Secret | Rôle |
| --- | --- | --- | --- | --- |
| `public_url` | aucun | oui | non | Public address; every absolute link is built from it. |
| `source.check_mode` | `always_on`, ou `piloted` quand un réveil (Sablier) est configuré | oui | non | How new chapters are looked for: always_on or piloted (EF-SRC-025). |
| `source.read_state_owner` | vide : l'administrateur | oui | non | The profile whose « read » is pushed back to Suwayomi (EF-SRC-026): a user name; empty is the administrator. A child is never pushed. |
| `export.missing_floor` | `10` | oui | non | Sentinel: files missing in a shelf destination before the share counts (EF-EXP-029). |
| `export.missing_ratio` | `0.05` | oui | non | Sentinel: share of missing files, above which (with the floor) exports, removals and scans are blocked (EF-EXP-029). |
| `export.missing_ceiling` | `50` | oui | non | Sentinel: more missing files than this block whatever the share (EF-EXP-029). |
| `instance.jurisdiction` | déduit de la langue du système | oui | non | Country whose copyright law drives the public-domain notices; the default is deduced from the locale (EF-SRC-024). |
| `maintenance.window` | `03:00-07:00 Europe/Paris` | non (lecture seule) | non | The default maintenance window (ENF-OPS-014), read only here: it is set by SHELFHOLD_MAINTENANCE_WINDOW_START, _END and SHELFHOLD_TIMEZONE. |
| `notify.urls` | aucun | oui | oui | Notification channels, name=url; the URL is sealed. |
| `path_map.<cible>` | selon la cible | oui | non | Correspondance des chemins de la destination (JSON) ; une clé par cible. |
