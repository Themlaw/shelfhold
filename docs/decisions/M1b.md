# Décisions d'exécution du jalon M1b

Ces décisions précisent ou amendent les ADR pour l'exécution de M1b. Une section par lot.

## L01 : schéma M1b (migration 0013)

La migration est la **0013** : la 0012 existe depuis la remédiation M1a L46. Les migrations 0001 à 0012 sont publiées (v0.2.0) et ne sont pas modifiées.

1. **Inventaire de ce que 0004 à 0012 ont déjà posé** (on ne crée que le manquant) :
   - `webauthn_credential` : absente. Créée.
   - `backup_run` : absente. Créée.
   - `source_config.instance_fingerprint` et `instance_fingerprint_seen_at` : absentes (seule `kavita_instance.fingerprint` existe, côté destination). Ajoutées.
   - Compteur d'erreurs de contenu consécutives : **déjà porté** par `source_failure.consecutive_content_errors`, par liaison, par `limit_key` et par classe d'échec (0003). Rien à créer.
   - `shelf.upstream_purge` : **déjà couvert** par `shelf.purge_upstream_after_export` (booléen, défaut faux, 0004), qui est le réglage de D16.
2. **Écart à D16 et au plan.** Le plan nomme la colonne `shelf.upstream_purge`. Une seconde colonne dirait la même chose que `purge_upstream_after_export` et pourrait la contredire ; renommer coûterait une reconstruction de table pour rien. Le réglage D16 est donc `shelf.purge_upstream_after_export`, désactivé par défaut, soumis à la mise à l'abri et à la double condition d'EF-BIB-038. Écart validé par l'orchestrateur.
3. **Garde de publication.** `tools/published-migrations.toml` n'est pas modifié : 0013 y sera ajoutée à la release suivante.
