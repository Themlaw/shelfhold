---
status: accepté (sous réserve du spike S1 en M0)
date: 2026-11-02
amended: 2026-11-02 (passe 1) ; 2026-11-02 (passe 2) ; 2026-11-03 (passe 11)
decision-makers: mainteneur
---
# ADR-0005 — File de jobs durable sur SQLite : Huey SqliteHuey épinglé + couche Shelfhold

> **Amendée le 2026-11-02 (passes 1 et 2).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Les acquisitions, vérifications de mises à jour, exports, scans et réconciliations tournent en arrière-plan. Ils doivent survivre à un redémarrage, reprendre un chapitre partiellement téléchargé, respecter des limites par source et des fenêtres horaires, et publier leur progression en SSE. 04-backend recommande un runner maison ; son avocat du diable rappelle qu'« une file maison, c'est comme ça que les projets meurent ».

## Facteurs de décision
- **Équité par domaine/source** : une source lente ne doit pas affamer les autres.
- **Reprise** après crash (lease, retries avec backoff et gigue, lettre morte, étapes enfants par page).
- **Progression SSE** fine (par page, par étape).
- Persistance SQLite (pas de Redis ni de Postgres), RAM, coût de maintenance.

## Options considérées
### A. Huey 3.5.x avec `SqliteStorage`
- Avantages : mature, déjà écrit et testé, SQLite intégré, planification (périodique) incluse, helper asyncio en contrib.
- Inconvénients : tâches sync/threadées ; pas de limites par domaine natives ; dédoublonnage et étapes enfants à construire par-dessus.
### B. Runner maison (~500 lignes)
- Avantages : table `jobs(kind, payload, status, attempts, run_after, lease_until, priority, dedupe_key, progress, parent_id)` ; claim par `UPDATE … RETURNING` ; pool asyncio avec limites par file et par source ; progression native vers le bus d'événements.
- Inconvénients : leases, horloge, jobs bloqués et messages empoisonnés sont des problèmes connus, à résoudre soi-même ; risque de bugs (R7).
### C. Procrastinate / SAQ / arq / Celery
- Avantages : excellents dans leur niche.
- Inconvénients : exigent Postgres ou Redis ; contraire à l'ADR-0004.

## Décision
*Texte d'origine (proposition, remplacé par l'amendement ci-dessous qui accepte Huey).* (D4). Faire un spike en M0, avec une limite de temps, en implémentant le même scénario de test sur A et sur B :
1. 3 sources, dont une lente, 50 jobs chacune : vérifier l'équité (aucune source affamée, plafond de concurrence par source respecté).
2. `kill -9` au milieu d'un chapitre de 30 pages : vérifier qu'il reprend sans retélécharger les pages faites.
3. Progression publiée en SSE par page.
4. Fenêtre horaire et retry avec backoff sur 429.

Règle : **à égalité, Huey** (ne pas réécrire une file). Le runner maison n'est retenu que si Huey échoue sur l'équité ou la reprise. Dans tous les cas, la limitation de débit par domaine vit dans le Fetcher (ADR-0007), pas dans la file, pour que la navigation interactive et les jobs partagent le même budget.

## Conséquences
- Positives : décision fondée sur des tests, pas sur une opinion ; ces tests rejoignent ensuite la suite permanente.
- Négatives : environ 2–4 jours de spike en M0 (estimation) ; si Huey est retenu, une couche d'adaptation est nécessaire pour la progression et l'équité.

## Risque principal
File maison buggée (R7), ou à l'inverse une couche Huey qui grossit jusqu'à devenir une file maison déguisée. Mitigation : port `JobQueue` dans `jobs/` pour que l'implémentation soit remplaçable, et tests de contrat communs.

## Réversibilité
Moyenne : grâce au port `JobQueue`, changer d'implémentation demande une migration des jobs en cours, mais aucune réécriture des modules appelants.

## Amendement passe 1 (2026-11-02)
- **Décision** (A12) : **Huey `SqliteHuey` épinglé `>=3.5.4,<3.6`** + une **couche Shelfhold** fine : slots de concurrence par source logique et par domaine (table `domain_slot`, la sémaphore native de Huey exigeant Redis), progression publiée vers le bus d'événements puis en SSE, idempotence au chapitre, étapes enfants par page. **Aucun runner maison** (option B rejetée, SIM-11 rejetée partiellement : la simplicité visée est obtenue par Huey, pas par une file écrite à la main).
- **Faits vérifiés** (09-closure-B) : Huey 3.5.4 publié le 2026-11-01, churn notable entre 3.5.0 et 3.5.4 (d'où l'épinglage strict) ; pas de prise en charge asyncio complète (helper en contrib seulement) ; priorités prises en charge ; tâches périodiques par crontab ; le `ttl` de `lock_task` **n'est pas pris en charge sur SQLite**, d'où un `flush_locks()` systématique au démarrage (mono-instance, ENF-OPS-007) ; `rate_limit` à fenêtre fixe seulement ; **une instance Huey par pool** (`io`, `cpu`, `maint`).
- **Alternatives réévaluées** : SAQ, Procrastinate, arq, dramatiq-sqlite, taskiq-sqlite ne sont pas mûrs sur SQLite (Procrastinate exige Postgres, arq/SAQ visent Redis, les backends SQLite tiers sont jeunes).
- **Spike S1 (≤ 1 jour, M0), 6 critères bloquants** : (1) 1 000 tâches sans « database is locked » ; (2) `kill -9` puis `flush_locks` et reprise sans retéléchargement des pages faites ; (3) tâche périodique à ±2 s sur 24 h ; (4) slot de domaine N=2 respecté ; (5) enqueue p99 < 5 ms ; (6) RSS au repos + 20 Mo au plus. Si un critère échoue, on documente le contournement dans la couche Shelfhold avant d'envisager autre chose.
- **Inconnue restante (levée en passe 2, voir l'amendement)** : comportement de rattrapage des ticks périodiques manqués pendant un arrêt ; mesuré dans le spike, et la couche Shelfhold rattrape explicitement les tâches périodiques dues au démarrage (EF-INT-003).
- La limitation de débit par domaine reste dans le Fetcher ; la clé de limitation par source logique amont (`limit_key`) vient des méta-sources (EF-SRC-021, A9).

## Amendement passe 2 (2026-11-02)
- **Rattrapage des tâches périodiques — fait vérifié** (`research/10-closure-C.md` §3, code de Huey 3.5.4, `consumer.py` `Scheduler.loop` et `api.py` `crontab`) : Huey ne rattrape **aucune** exécution manquée. Le crontab est évalué sans état sur la minute courante toutes les 60 s ; les vérifications manquées (arrêt, suspension) sont sautées volontairement ; une minute peut être vue deux fois ou sautée en cas de gigue.
- **Décision** (B24) : chaque périodique stocke `last_run_at` en base ; au démarrage, après `flush_locks`, une tâche de rattrapage enfile **une seule fois** chaque périodique dont l'échéance est dépassée (politique *run-once*, jamais N exécutions) ; toutes les périodiques sont idempotentes.
- **Spike S1, critère 3 remplacé** : « arrêt de 2 h puis redémarrage → exactement une exécution de rattrapage par périodique » (au lieu d'« observer le rattrapage »). La dérive ≤ ± 2 s sur 24 h reste mesurée.
- Les jobs qui touchent `/store` ou `/library` sont placés dans la **fenêtre de maintenance** (ENF-OPS-014, ADR-0028).
- Huey est inscrit dans `docs/dependencies.md` (facteur bus 91 %, repli : runner maison derrière le port `JobQueue`, Q3 ; ENF-GOV-002).

## Amendement passe 11 (2026-11-03)
L6 (Z2), M1b. Trois **classes de priorité Huey fixes** : nouveautés suivies > demandes approuvées > rattrapage (`backfill`) ; un rattrapage est découpé en lots par cycle pour laisser passer les autres travaux ; champ additif `job_view.request_id?` (« pour qui » dans l'Activité). Pas de quota disque par utilisateur : le modèle est partagé et dédoublonné. Les jobs étant éphémères, ce réglage ne demande aucune migration (`test_huey_priority_classes_new_before_backfill`).

## Exigences liées
EF-INT-003, EF-INT-004, EF-INT-002, EF-SRC-009, EF-SRC-021, EF-BIB-012, ENF-OPS-007, ENF-PERF-001, EF-INT-017.

## Sources
- https://github.com/coleifer/huey
- https://github.com/procrastinate-org/procrastinate
- https://github.com/tobymao/saq
- https://github.com/riverqueue/river
- https://github.com/graphile/worker
- https://github.com/coleifer/huey/blob/master/CHANGELOG.md
- https://github.com/coleifer/huey/blob/master/docs/api.rst
- https://github.com/coleifer/huey/blob/master/docs/asyncio.rst
- https://github.com/coleifer/huey/blob/master/docs/consumer.rst
- https://github.com/coleifer/huey/blob/master/huey/contrib/helpers.py
- https://github.com/tobymao/saq/tree/master/saq/queue
- https://github.com/hyperflask/dramatiq-sqlite
- https://github.com/Ash1One/taskiq-sqlite
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)
