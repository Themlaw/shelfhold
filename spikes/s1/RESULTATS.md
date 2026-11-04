# Spike S1 : Huey `SqliteHuey` (ADR-0005)

Mesuré le 2026-11-04 sur Linux x86_64, Python 3.13.15, **Huey 3.5.4** (`>=3.5.4,<3.6`), consommateur à fils
(`huey_consumer -k thread`). Code jetable : `tasks.py`, `layer.py` (prototype de la couche Shelfhold :
`domain_slot`, `last_run_at`), `run_s1.py` (critères 1, 2, 4, 5, 6 -> `results.json`), `periodic.py`
(critère 3 -> `periodic.json`). Une instance Huey par pool (`io`, `cpu`, `maint`), un fichier SQLite chacune.

| # | Critère | Résultat | Verdict |
| --- | --- | --- | --- |
| 1 | 1 000 tâches sans « database is locked » | 1 000 / 1 000 exécutées (4 producteurs, 8 fils, chaque tâche écrit dans une base applicative WAL) ; 0 occurrence de `locked` (journal consommateur et enfilement) ; enfilement 0,12 s, exécution 1,12 s | **passe** |
| 2 | `kill -9` puis `flush_locks` | Le verrou `lock_task` survit au `kill -9` (constaté) ; redémarré **sans** `flush_locks`, les 5 tâches restantes échouent toutes sur le verrou orphelin (0 exécutée) ; avec `flush_locks` au démarrage, file vidée, aucun verrou résiduel. La tâche en cours au moment du kill est perdue pour Huey (pas d'acquittement) : la table `jobs` de Shelfhold (statut `running`) la remet en file, idempotente par chapitre | **passe** (avec la remarque) |
| 3 | Périodique : dérive <= +-2 s ; arrêt puis redémarrage -> **une** exécution de rattrapage | Rattrapage : Huey n'exécute **aucun** tick manqué (0 pendant 200 s d'arrêt, 3 ticks manqués) ; la couche (`last_run_at`, politique *run-once*) en enfile exactement **1** ; 1 exécution `cron` dans la première minute après redémarrage. Précision : écart au passage de minute de **-28 à +11 s** sur 8 ticks | **ÉCHOUÉ** pour la précision (-28 à +11 s mesurés sur 9,5 min, cible +-2 s) ; le rattrapage seul passe |
| 4 | Slot par domaine N = 2 | Pic de concurrence 2 / 2 / 2 sur 3 domaines, 60 tâches sur 8 fils, toutes exécutées, jamais 3 | **passe** |
| 5 | Latence d'enfilement p99 < 5 ms | 3 000 enfilements : consommateur inactif p50 0,045 ms, p99 **0,129 ms**, max 1,3 ms ; consommateur chargé (8 fils) p50 0,045 ms, p99 **0,155 ms**, max 8,5 ms | **passe** |
| 6 | Surcoût RSS au repos <= +20 Mo | Mesure en processus (3 pools : io 4 fils, cpu 1, maint 1) sur la pile du backend : 62,2 Mo -> 65,6 Mo, soit **+3,4 Mo** (3 runs : 3,3 / 3,4 / 3,5) | **passe** |

## Limites honnêtes de la mesure du critère 3
- **Version accélérée, pas 24 h.** Tâche à la minute pendant 9,5 minutes (572 s) : 5 ticks, 200 s d'arrêt, 3 ticks
  après redémarrage. Une dérive cumulée lente (horloge, NTP) sur 24 h n'est donc **pas** observable ici ; elle doit
  être vérifiée par un essai long (nocturne ou manuel) avant la 1.0.
- **Ce que l'on observe n'est pas une dérive mais un décalage de phase et une gigue.** Huey évalue le crontab
  toutes les 60 s à partir du démarrage du consommateur, pas sur la frontière de minute : le décalage dépend de
  l'instant de démarrage (+4 à +11 s, puis -21 à -28 s après le redémarrage) et la gigue entre deux ticks atteint
  environ 6 s dans cette session (4,6 ; 11,1 ; 8,8 ; 6,5 ; 11,1). La machine n'était pas isolée (autres conteneurs).
- Le critère « +-2 s » n'est donc pas tenu par Huey pour un crontab à la minute. Il n'a pas de valeur produit :
  les périodiques de Shelfhold (vérification de mises à jour, entretien, réconciliation) sont horaires ou
  quotidiennes et idempotentes.
- Le rattrapage est testé avec 3 ticks manqués et non 2 h ; la logique (`now - last_run_at > 1,5 x intervalle`)
  ne dépend pas de la durée.
- Le slot de domaine du prototype attend en boucle dans le fil de travail ; en production, la tâche se
  réenfile avec un délai pour ne pas occuper un fil. Le critère porte sur le plafond de concurrence, pas sur cela.
- Critère 2 : la perte de la tâche en cours est une propriété de Huey (retrait de la file avant exécution) ;
  elle impose que chaque tâche soit idempotente et suivie dans `jobs`, ce que l'ADR prévoit déjà.

## Lecture du code de Huey 3.5.4 : un tick de crontab peut-il être sauté ?
Oui. `Scheduler.loop` (`huey/consumer.py`) réveille le planificateur toutes les 60 s sur l'horloge
**monotone** (`_next_periodic += 60`), à partir du démarrage du consommateur et non sur la frontière de minute ;
`read_periodic(now)` évalue ensuite le crontab sur la minute **murale** courante. Deux conséquences, établies par
lecture du code et cohérentes avec le décalage de phase mesuré :
1. si deux réveils consécutifs tombent de part et d'autre d'une minute (gigue, phase proche de la frontière), la
   minute intermédiaire n'est évaluée par aucun réveil : **un tick peut être sauté** (et, symétriquement, une minute
   peut être vue deux fois ; le dédoublonnage est à la charge de la tâche) ;
2. après un arrêt du processus ou une suspension, les réveils manqués sont volontairement ignorés (`while
   self._next_periodic <= time.monotonic()`), ce qui confirme l'absence de rattrapage.
Je n'ai pas observé de tick sauté dans les 8 ticks mesurés (ce n'est pas une preuve d'absence sur 24 h).
**Conclusion** : un `crontab(minute=0)` horaire peut, rarement, ne pas se déclencher à l'heure ; la couche
Shelfhold ne doit donc pas se reposer sur le seul crontab : elle scrute `last_run_at` (échéance dépassée de plus
de 1,5 intervalle -> une exécution, politique run-once) au démarrage **et** à chaque minute.

## Conclusion
Critères 1, 2, 4, 5 et 6 : passés. **Critère 3 : ÉCHOUÉ** (précision +-2 s ; seul le rattrapage au redémarrage passe).
Décision : **Huey est retenu malgré tout**, parce que les périodiques de Shelfhold sont horaires ou quotidiennes,
avec `jitter_s` (02-architecture, table `schedule`), idempotentes, sans exigence à la seconde, et que le critère
a été écrit pour un risque (rattrapage, dérive) que la couche `last_run_at` couvre. Aucune raison de revenir à
un runner maison.

**Dette explicite, à solder avant la 1.0** : mesure sur 24 h (dérive cumulative, ticks sautés) du crontab
et de la couche de rattrapage, hors CI (nocturne ou manuelle), avec le critère reformulé « gigue <= 15 s, aucun
tick perdu non rattrapé en 24 h ».
