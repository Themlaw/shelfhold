# Métriques (`/metrics`)

Shelfhold peut exposer ses chiffres au format **Prometheus** (texte 0.0.4) sur `GET /metrics`.
C'est un interrupteur d'hôte : la route est **éteinte par défaut** et aucune action HTTP ne
l'allume.

## L'allumer

`SHELFHOLD_METRICS_ENABLED=true` dans l'environnement du conteneur (le `.env` du compose, voir
[configuration.md](../configuration.md)), puis redémarrage. Rien d'autre : ce n'est pas une
action réservée, puisqu'aucune route ne bascule l'interrupteur.

## Qui peut la lire : une clé, une seule portée

`/metrics` ne se lit qu'avec une **clé d'API de portée `metrics`**, créée par l'administrateur
pour lui-même :

```sh
shelfhold admin keys create --scope metrics
```

Cette clé ne lit que `/metrics` et rien d'autre ; une session (celle de l'administrateur
comprise), un jeton d'appareil ou une clé d'une autre portée reçoivent `403` sans rien apprendre
de la route. Tant que la route est éteinte, même la bonne clé reçoit `404` : l'instance ne dit
pas qu'elle pourrait répondre. Les clés d'API se gèrent comme les autres (liste, rotation,
révocation) : voir [roles.md](roles.md) et [durcir.md](durcir.md).

## Ce que le contenu dit — et ne dira jamais

Les familles exposées : `shelfhold_build_info` (avec l'étiquette `version`),
`shelfhold_jobs` et `shelfhold_job_duration_seconds` (les tâches, par état et durées),
`shelfhold_export_plans` (les plans d'export, par état), `shelfhold_requests` (les demandes, par
état), `shelfhold_rate_limit_slots` et `shelfhold_rate_limit_slots_in_use` (les créneaux de
limite de débit), `shelfhold_units` et `shelfhold_works` (les tailles de la bibliothèque et du
catalogue).

Deux étiquettes seulement existent (`state`, sur une liste fermée du schéma, et `version`) : le
contenu est sûr par sa forme, pas par un filtre. **Rien** ne nomme une personne, un titre, une
série, une source, un hôte, un chemin ou une adresse — un compteur par domaine ou par source
n'existera pas, un hôte désigne un lieu de contenu. Un état hors liste est écarté, jamais
imprimé.

## Pour les chiffres que `/metrics` ne porte pas

L'espace par étagère, le trafic par source, l'historique d'activité et la liste de blocage sont
sur l'écran d'administration, pas dans la sortie Prometheus :
[historique-et-stockage.md](historique-et-stockage.md). Les journaux et le paquet de diagnostic
sont dans [journaux.md](journaux.md) et [diagnostic.md](diagnostic.md).
