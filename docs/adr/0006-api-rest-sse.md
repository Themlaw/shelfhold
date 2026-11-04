---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 1) ; 2026-11-03 (passe 12)
decision-makers: mainteneur
---
# ADR-0006 — API REST + OpenAPI 3.1, temps réel SSE, protocoles standards

> **Amendée le 2026-11-02 (passe 1).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
L'API sert l'UI (client TS), la CLI, des intégrations (webhooks, Home Assistant), des liseuses (OPDS, kosync) et, plus tard, des clients tiers (Mihon/Komga). Il faut un contrat typé, stable et versionné, ainsi qu'un canal temps réel pour la file de jobs.

## Facteurs de décision
- Client TS généré pour éviter toute dérive entre front et back.
- Simplicité du cache (ETag, `?since=`).
- Interopérabilité avec des standards existants (OPDS, kosync).
- Temps réel unidirectionnel suffisant.

## Options considérées
### A. REST + OpenAPI 3.1 (FastAPI), client généré (openapi-typescript/hey-api), SSE
- Avantages : modèle Immich (OpenAPI généré, SDK TS, check en CI) ; cache HTTP natif ; SSE simple, avec reprise par `Last-Event-ID`.
- Inconvénients : risque de prolifération d'endpoints.
### B. GraphQL
- Avantages : requêtes flexibles (le choix de Suwayomi, qui a de nombreux clients hétérogènes).
- Inconvénients : N+1, cache complexe ; pas de diversité de clients qui le justifie ici.
### C. tRPC / WebSockets
- Avantages : typage de bout en bout ; WebSockets bidirectionnels.
- Inconvénients : tRPC lie l'API à un backend TS ; les WebSockets ne sont pas nécessaires pour des flux serveur→client.

## Décision
Option A (D5, D37) : `/api/v1` versionnée, OpenAPI 3.1, ETag et `?since=` ; SSE `/api/v1/events` typé ; OPDS 1.2 + PSE + 2.0 sortants ; kosync (`/users/create`, `/users/auth`, `PUT /syncs/progress`, `GET /syncs/progress/:document`) ; webhooks sortants signés HMAC via l'outbox ; notifications Apprise. En V2 : sous-ensemble compatible Komga **ou** extension Mihon dédiée (choix au jalon M6).

## Conséquences
- Positives : le client TS est vérifié en CI (`check-openapi`) ; les standards rendent KOReader et les apps OPDS utilisables sans travail côté client.
- Négatives : plusieurs surfaces à sécuriser de manière homogène (scope de visibilité, ADR-0018) ; une politique de dépréciation est nécessaire.

## Risque principal
Une surface (OPDS, kosync) qui contourne le scope de visibilité. Mitigation : test paramétré généré depuis l'OpenAPI (ENF-SEC-010).

## Réversibilité
Moyenne : l'API publique engage les clients ; le versionnement `/v1` permet d'évoluer.

## Amendement passe 1 (2026-11-02)
- **Calendrier des surfaces** (A1, A3) : V1 = `/api/v1` (OpenAPI 3.1, RFC 9457, pagination par curseur, clés d'idempotence, `If-Match`), SSE typé `/api/v1/events` (M3, heartbeat 15 s, `X-Accel-Buffering: no`), notifications Apprise. **OPDS et kosync propres à Shelfhold passent en M5** : en V1 la famille utilise l'OPDS et le kosync **de Kavita**. Webhooks HMAC via outbox en M4 (EF-INT-005), avec `schema_version` et double émission pendant une dépréciation (EVO-41).
- **kosync** (closure-A) : en-têtes `x-auth-user`, `x-auth-key` (= md5 du mot de passe), `accept: application/vnd.koreader.v1+json` ; 4 routes ; `users/create` désactivé ; hash calculé sur le fichier exporté (EF-LEC-009, ENF-QUAL-005).
- **Jetons en URL** (A28) : uniquement pour OPDS/kosync, hachés, en lecture seule, scopés, révocables, rédigés des logs (ENF-SEC-013).
- **Compatibilité** : versionnement `/v1` + politique de dépréciation publiée (ADR-0026).

## Amendement passe 4 (2026-11-03)
Règles stables de l'API (D13, GAP-4-16) : un plan périmé renvoie **412** (`If-Match`), 409 étant réservé aux conflits métier ; opérations longues en `202 Accepted` + `Location: /api/v1/jobs/{id}` (le SSE n'arrive qu'en M3) ; clés d'idempotence conservées 24 h ; le `type` RFC 9457 est l'URI stable du code d'erreur, qui est aussi l'identifiant de l'événement (EF-UX-003) ; `ETag` et `?since=` sur les listes ; CORS fermé par défaut ; champs amont marqués non fiables dans l'OpenAPI ; **dépréciation** par en-têtes `Deprecation` et `Sunset`, au moins une version mineure et 6 mois après 1.0. Surface d'exploitation (D5) : `GET /api/v1/status` et une route de sortie par état d'attente (EF-INT-023).

## Exigences liées
EF-INT-001, EF-INT-002, EF-INT-005, EF-INT-006, EF-INT-007, EF-INT-011, EF-LEC-009, ENF-SEC-010, ENF-SEC-013, EF-INT-023, EF-UX-003, EF-EXP-042.

## Sources
- https://github.com/immich-app/immich
- https://github.com/koreader/koreader-sync-server
- https://github.com/opds-community/drafts
- https://github.com/anansi-project/opds-pse
- https://github.com/caronc/apprise
- https://github.com/Suwayomi/Suwayomi-Server
- https://github.com/koreader/koreader/blob/master/plugins/kosync.koplugin/api.json
- https://github.com/koreader/koreader/blob/master/plugins/kosync.koplugin/KOSyncClient.lua
- `deliverables/review/chasseur-trous-passe4.md` (GAP-4-03, GAP-4-16)
- https://www.rfc-editor.org/rfc/rfc9111
- `deliverables/review/ARBITRAGES-passe4.md`

## Amendement passe 12 (2026-11-03)
M10 (AA4). À fixer avant le gel 1.0 (M3), car c'est de la sémantique et pas de la forme :

- `?since=` prend un **jeton opaque** adossé à la séquence de la table `event`, jamais un horodatage.
- La réponse porte aussi les **retraits** : suppression, `purge --now`, fusion et **perte de visibilité pour le lecteur** (reclassement, étagère retirée, I48).
- Un jeton plus ancien que la rétention des événements (90 j) renvoie `…/problems/resync-required`, symétrique de `resync_required` du SSE.
- Ajouts additifs : un client hors ligne voit disparaître ce qu'il a en cache (éviction de la PWA, ADR-0012).

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)

## Amendement passe 13 (2026-11-03)
N5 (GAP-13-08).

- Les retraits renvoyés par `?since=` se limitent aux objets **déjà visibles** du lecteur au moment du jeton (visibilité rejouée depuis les événements de classement et d'étagère) : un profil enfant n'apprend jamais l'existence d'une Œuvre qu'il n'a jamais vue. Avant le gel 1.0 (M3).
- Test : `test_since_removals_never_list_items_never_visible_to_viewer`.

Source : `deliverables/review/chasseur-trous-passe13.md`, `deliverables/review/explorateur-passe13.md`, `deliverables/review/ARBITRAGES-passe13.md` (passe 13)
