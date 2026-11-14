# Journaux : niveaux, noms d'événements et lecture

Shelfhold écrit ses journaux sur la sortie standard du conteneur, une ligne JSON par événement (`SHELFHOLD_LOG_FORMAT=json`, par défaut) ou un texte lisible (`console`, pour le développement). Le niveau se règle avec `SHELFHOLD_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING` ou `ERROR`) ; `docker compose logs shelfhold` suffit à les lire. Les commandes `shelfhold …` lancées à la main écrivent leurs journaux sur la sortie d'erreur : la sortie standard reste celle de la commande (un document JSON avec `--json`, une liste), sans ligne de journal mêlée.

## Les niveaux

| Niveau | Ce qu'on y trouve | À utiliser pour |
| --- | --- | --- |
| `ERROR` | Un job qui échoue, une intention ou une écriture refusée, une clé Kavita révoquée | Être alerté |
| `WARNING` | Une anomalie rattrapée : réessai (`job.retry`), saut, refus d'une garde d'écriture (`fsops.write_refused`), appel réseau en échec (`http.failed`, `kavita.unreachable`), connexion refusée | Repérer ce qui se dégrade |
| `INFO` | Début et fin de chaque job (`job.started`, `job.finished`, avec `duration_ms` et des compteurs), chaque changement d'état métier (plan d'export, rapprochement qui trouve quelque chose, purge, rapatriement, adoption), la configuration effective au démarrage, les actions réservées et les réauthentifications | Fonctionnement normal |
| `DEBUG` | Chaque décision et chaque appel externe, avec ses arguments sans secret : chapitres vus, demandés, collectés ; chaque opération d'un plan d'export ; méthode, route, statut et durée de chaque appel à Kavita et à Suwayomi ; chaque écriture, renommage et suppression de fichier ; les sondes de santé | Comprendre un cas précis |

En marche nominale, `INFO` reste calme : un cycle de suivi sans nouveauté écrit deux lignes (`job.started` et `job.finished`), et son résumé détaillé (`follow.cycle_summary`) ne passe en `INFO` que s'il a trouvé ou fait quelque chose. Passez en `DEBUG` le temps d'un diagnostic, puis revenez à `INFO` : le niveau `DEBUG` est volumineux.

## Convention des noms

Chaque événement s'appelle `contexte.action` : des mots en minuscules, séparés par des points ou des tirets bas (`follow.cycle_summary`, `export.plan_computed`, `fsops.write_atomic`). Le contexte est le domaine (`job`, `follow`, `discovery`, `export`, `reconcile`, `purge`, `repatriate`, `adopt`, `tombstone`, `kavita`, `http`, `fsops`, `auth`, `startup`), l'action ce qui vient de se passer.

Le champ `duration_ms` donne la durée en millisecondes de tout ce qui a une fin : un job, un appel réseau, l'application d'un plan, un rapprochement, une purge.

## Les champs liés

Les lignes émises dans la portée d'un job portent ces champs, sans qu'il faille les répéter :

| Champ | Portée |
| --- | --- |
| `job_id`, `job_kind` | tout ce que fait un job, de `job.started` à `job.finished` |
| `series_id`, `edition_id`, `binding_id` | l'examen d'une série par le cycle de suivi |
| `unit_id` | chaque opération d'un plan d'export |
| `target_id`, `destination_id` | l'export d'une destination Kavita |
| `root_id` | les décisions qui touchent une racine physique (purge, pierres tombales) |
| `request_id` | une requête HTTP (déjà en place) |

`job.finished` porte aussi `outcome` (`ok`, `error`, `failed`, `parked`, `preempted`) et les compteurs du job. Suivre un job de bout en bout se fait donc avec son identifiant, par exemple `docker compose logs shelfhold | grep '"job_id": "<identifiant>"'`.

## Ce que les journaux ne contiennent jamais

- **Aucun secret, à aucun niveau** : ni la clé Kavita, ni les identifiants Suwayomi, ni jeton, cookie, en-tête `Authorization` ou `x-api-key`, ni clé du trousseau, ni mot de passe. Un filtre masque en plus, par nom de champ et par motif, ce qui aurait échappé au code ; les traces d'erreur passent par le même filtre.
- **Aucune query string** : les appels HTTP sont journalisés avec l'hôte et la route seulement (`http.call`). Une route qui porte la clé dans son adresse n'est journalisée que par son libellé. La bibliothèque HTTP elle-même est maintenue au niveau `WARNING`, parce que ses lignes `INFO` citent l'adresse complète.
- **Aucun nom de fichier issu d'une archive non fiable au-dessus de `DEBUG`** : en `DEBUG`, le nom est échappé (les retours à la ligne et les caractères de contrôle apparaissent sous leur forme écrite, par exemple `\n`), et jamais interprété.
- **Aucun titre ni valeur de pierre tombale** : les journaux de purge et de pierres tombales ne donnent que des comptes et des identifiants.

## Lire un cas fréquent

- *Pourquoi ce chapitre n'est-il pas arrivé ?* En `DEBUG`, cherchez `follow.series_looked` (ce que la série a listé), `follow.ticket_requested` et `follow.ticket_polled` (la demande à Suwayomi), `follow.entry_refused` (une archive refusée), `follow.budget_exhausted` (le plafond du cycle).
- *Pourquoi rien n'est-il écrit dans Kavita ?* `export.nothing_to_write`, `export.plan_empty` (avec les codes bloquants), `export.series_paused`, `export.write_denied`, `fsops.write_refused`.
- *Un job réessaie sans fin ?* `job.retry` donne la tentative et le délai, `job.dead_lettered` l'abandon, et `job.failure_detail` (en `DEBUG`) la classe d'erreur, les lignes de code traversées et le message échappé.
