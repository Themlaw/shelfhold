# Caches dérivés : vignettes, dérivés de lecture, cache HTTP, métadonnées

Shelfhold garde quatre caches **dérivés** : des données qu'il peut recréer à la demande, jamais
l'original d'un livre. Ils sont bornés par un quota, nettoyés par âge et par dernier accès
(LRU), et l'hôte n'écrit que dans des chemins qu'il a créés lui-même.

## Les quatre caches

| Cache | Ce qu'il contient | Quota par défaut |
| --- | --- | --- |
| vignettes | vignettes de couverture | 2048 Mio |
| dérivés de lecture | rendus (KEPUB compris) | 5120 Mio |
| cache HTTP | corps des réponses HTTP | 512 Mio |
| métadonnées | réponses des fournisseurs de métadonnées | 256 Mio et 4096 lignes |

## Quotas et rétention

Les réglages (dans la configuration, voir `docs/reference/config.md`) :

- `cache_thumbnails_quota_mib`, `cache_thumbnails_retention_days` (défaut 2048 Mio, 90 jours)
- `cache_renditions_quota_mib`, `cache_renditions_retention_days` (défaut 5120 Mio, 90 jours)
- `cache_http_quota_mib`, `cache_http_retention_days` (défaut 512 Mio, 90 jours)
- `cache_metadata_rows`, `cache_metadata_quota_mib` (défaut 4096 lignes, 256 Mio)
- `cache_plugin_quota_mib` (défaut 256 Mio)

Le job `retention.caches` (quotidien) applique le quota et la rétention, en LRU par dernier accès.
Un cache dépassé est signalé par `shelfhold status`.

## Voir l'état des caches

```sh
shelfhold status
```

La section des caches montre, pour chacun, le nombre d'entrées, l'octets utilisé et le quota, avec
la mention « dépassé » le cas échéant. Le cache de métadonnées est compté en lignes.

## Ce que le cache n'est pas

- Rien d'irremplaçable n'y vit : un cache effacé se reconstruit à la demande.
- L'hôte ne crée que ses propres chemins : pas d'écriture dans un dossier fourni par l'utilisateur,
  pas de chemin hérité d'une version antérieure.
- Vider un cache ne perd aucun livre : le livre reste dans `/store` et dans la base.

Voir aussi [garde-disque.md](garde-disque.md), [historique-et-stockage.md](historique-et-stockage.md).