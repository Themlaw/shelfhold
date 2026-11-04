---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 1) ; 2026-11-02 (passe 2) ; 2026-11-03 (passe 5) ; 2026-11-03 (passe 6) ; 2026-11-04 (spike S5)
decision-makers: mainteneur
---
# ADR-0008 — Méta-sources (Suwayomi, lncrawl, OPDS) plutôt qu'embarquer la JVM

> **Amendée le 2026-11-02 (passes 1 et 2).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Le plus grand corpus de scrapers manga maintenus, ce sont les extensions Mihon (keiyoushi), sous forme d'APK Android. Les romans web sont couverts par lncrawl (446 crawlers). Réimplémenter l'un ou l'autre est hors de portée d'un mainteneur unique.

## Facteurs de décision
- Couverture du catalogue sans maintenir de scrapers.
- RAM et complexité du conteneur principal.
- Exposition légale (ne livrer aucune source).
- Stabilité des API externes.

## Options considérées
### A. Méta-sources : adaptateurs vers des outils installés par l'utilisateur (Suwayomi GraphQL, serveur lncrawl REST ou import du paquet, client OPDS)
- Avantages : tout le catalogue Mihon via Suwayomi (FlareSolverr intégré, OPDS, téléchargement CBZ `GET /api/v1/chapter/{id}/download`) ; modèle prouvé par Kaizoku/Rensaio ; aucune source livrée.
- Inconvénients : deux processus et deux bases (l'état Suwayomi devient un cache) ; dérive possible du schéma GraphQL ; authentification multi-utilisateur de Suwayomi ; impossible d'intercepter sa couche HTTP.
### B. Embarquer la JVM et le shim Android (dex2jar + AndroidCompat)
- Avantages : un seul modèle de données, pas de couche de synchro.
- Inconvénients : c'est le travail à plein temps de Suwayomi ; churn des ext-lib 1.4→1.6 ; 300 Mo+ de JVM dans le conteneur principal.
### C. Réécrire les sources en Python natif
- Avantages : contrôle total.
- Inconvénients : maintenance hebdomadaire des sites cassés ; exposition légale.

## Décision
Option A (D9). Suwayomi : **une source hôte par source Suwayomi**, clé `(sourceId, SManga.url)`, jamais l'`mangaId` entier local ; modes (a) pages via proxy et (b) CBZ téléchargé. lncrawl : **serveur REST piloté par jobs, hors processus** (défaut retenu) ; l'import du paquet en processus est écarté car ses crawlers ouvrent leurs propres connexions, contournant le Fetcher (EF-SRC-002), et couplerait les versions (voir Q13 ; la question de licence est traitée dans ADR-0014). OPDS générique pour Kavita, Komga, Calibre-Web et le flux OPDS de Suwayomi. Versions épinglées et tests de contrat GraphQL et REST sur cassettes.

## Conséquences
- Positives : V1 utile sans écrire de scraper manga ; posture légale neutre ; le conteneur principal reste léger (Suwayomi est un sidecar optionnel).
- Négatives : il faut réconcilier deux états ; double copie sur disque si on utilise le téléchargeur Suwayomi (mode b) ; dépendance au rythme de publication de Suwayomi.

## Risque principal
Dérive des API (R1). Mitigation : version épinglée dans le compose de référence, nightly sur `latest` non bloquant, tests de contrat sur les requêtes GraphQL utilisées.

## Réversibilité
Moyenne : un adaptateur est remplaçable, mais un SourceItem préfixé `suwayomi:` demanderait une migration de source (EF-BIB-014) si on change de moteur.

## Amendement passe 1 (2026-11-02)
- **Suwayomi en V1 = CBZ seulement** (A2, SIM-05) : téléchargement par `GET /api/v1/chapter/{id}/download`, **jamais page par page** en V1 (le mode « pages via proxy » est retiré du périmètre V1). SourceItem `(suwayomi, suwayomi:<sourceId>, <manga.url>)`. La bibliothèque Suwayomi fait foi pour le suivi manga ; Shelfhold interroge Suwayomi (GraphQL), demande le téléchargement et récupère le CBZ (EF-SRC-009).
- **Sous-sources dynamiques** (A9, EF-SRC-021) : la méta-source expose N sous-sources (`sub_sources()`, `content_warning` par sous-source) ; la limitation de débit se fait par **source logique amont** (`limit_key` fourni par la méta-source) en plus du domaine.
- **lncrawl** (A19, EF-SRC-010) : adaptateur vers **l'instance de l'utilisateur** uniquement ; EPUB passthrough ; Shelfhold n'active ni ne liste aucun crawler, et le compose de référence ne contient pas de profil lncrawl (LEG-04).
- **Méta-sources livrées sans aucune source active** (ENF-LEG-001).
- **Réveil à la demande** : si Suwayomi est derrière Sablier, Shelfhold le réveille avant un lot via le port `WakeHook` (ADR-0025, EF-INT-022).
- **Honnêteté du déploiement** (ENF-OPS-005) : le cas de référence = Shelfhold + Suwayomi (≥ 750 Mo, KCEF 320–500 Mo) + Kavita.
- **Hors cœur, confirmé** (closure-A) : Torznab/Newznab reste « Jamais » dans le cœur ; le contrat des fournisseurs de métadonnées reprend la *forme* de l'API `Source` de Calibre (capabilities, touched_fields), pas son code GPL (ADR-0024).

## Amendement passe 2 (2026-11-02)
- **Mode passage** (ADR-0028, B6) : par défaut, le CBZ récupéré de Suwayomi passe par le staging et est rangé ; Shelfhold ne garde pas de troisième copie. Option par étagère « purger le téléchargement Suwayomi après export réussi » (API de suppression du chapitre téléchargé), désactivée par défaut.
- **Qui interroge l'amont** (GAP-2-18) : Suwayomi rafraîchit l'amont selon sa propre planification ; Shelfhold ne lit que le GraphQL local et ne fait que **demander** des téléchargements ; la limitation par `limit_key` s'applique à ces demandes.
- **Adoption reliée au suivi** (B7, EF-EXP-038) ; **préférence d'équipe minimale** dès M1 (B8, EF-BIB-028) ; **migration de source** : `shelfhold relink` en M1 (B9, EF-BIB-029).
- **AuthFlow Basic avancé en M1** pour un Suwayomi protégé (GAP-2-30).
- **Contrat de la route de téléchargement** : le fait que `GET /api/v1/chapter/{id}/download` renvoie bien une archive CBZ est vérifié par un test de contrat contre un Suwayomi réel épinglé dès M0 (spike S5, B16), puis en nightly.

## Amendement passe 3 (2026-11-02)
- **Vérification des nouveautés pilotée** (C5, EF-SRC-025) : un Suwayomi endormi par Sablier n'exécute pas sa propre planification ; « Suwayomi rafraîchit l'amont selon sa propre planification » n'est donc vrai qu'en mode « toujours actif ». Réglage explicite : mode **piloté** (défaut avec WakeHook) où Shelfhold réveille Suwayomi puis lui demande le rafraîchissement des séries suivies, ou mode **toujours actif**.
- **Sonde de santé fonctionnelle** (C15, EF-SRC-026) : un Suwayomi joignable peut être muet (extension non chargée ou à moitié mise à jour, chapitres sortis de la file après trois échecs, filtres de mise à jour globale qui sautent les séries « non lues » alors qu'on lit dans Kavita). La méta-source vérifie les sources chargées, la fraîcheur par série, le dernier téléchargement réussi, réenfile les chapitres abandonnés (plafond) et bloque l'assistant si un filtre global est actif. Shelfhold n'installe ni ne met à jour aucune extension (posture ADR-0015). En M4, renvoi de l'état « lu » vers Suwayomi.
- **Filtrage par équipe dès le téléchargement** (C16, EF-BIB-035) : Suwayomi télécharge délibérément toutes les versions d'un chapitre ; Shelfhold n'enfile que la Variante retenue.
- **Plage de versions** (C13, ENF-COMP-001) : version lue par `aboutServer` au démarrage ; hors plage → `source.version_unsupported`, pas « injoignable ».
- **Série retirée, chapitre pas prêt** (EF-SRC-027, EF-SRC-028) : aucune suppression en cascade ; attente de l'état « téléchargé » sans compter d'échec de source.

## Amendement passe 4 (2026-11-03)
Suwayomi (D6, D7, D13, GAP-4-04, GAP-4-05, GAP-4-06, R7) : (1) Shelfhold **écrit** dans la bibliothèque Suwayomi par `follow`/`unfollow` idempotents, dans une catégorie « Shelfhold » exclue du téléchargement automatique ; une série déjà présente est rattachée (EF-SRC-029, M1b) ; (2) la relance de la sonde fonctionnelle est **limitée aux chapitres demandés par Shelfhold** (`download_request`), jamais les autres équipes ni l'arriéré (EF-SRC-026) ; (3) un 401 (`AuthExpired`), un schéma changé ou une version hors plage mettent le service en pause sans compter d'erreur de contenu (EF-BIB-042) ; (4) le spike S5 vérifie aussi le chemin **GraphQL** équivalent au téléchargement CBZ, en prévision d'un retrait de l'API REST ; (5) M1a fonctionne en mode « toujours actif » seulement, le mode piloté arrive en M1b avec le WakeHook.

## Amendement passe 5 (2026-11-03)
Arbitrage E12 (T1) et GAP-5-12.
- **Suwayomi multi-comptes** (`ui_login`, EF-SRC-031) : détecté et refusé en M1a ; compte de service et AuthFlow JWT en M1b ; « purge » = retrait de la revendication de Shelfhold, espace libéré mesuré. Spike S5 étendu à `basic_auth` et `ui_login`, version épinglée et branche principale ; ligne « Suwayomi multi-comptes » dans `docs/dependencies.md` (signal : première stable incluant #623).
- **Fraîcheur par série** (volet c d'EF-SRC-026) avancée en M1a, valeurs par défaut N = 6 h et k = 3 ; c'est la détection d'un Suwayomi muet en mode « toujours actif ».
- **Premier cycle** : nouveautés uniquement, plafond de demandes par cycle et par `limit_key` (EF-EXP-053).

## Amendement passe 6 (2026-11-03)
F9, F12 (GAP-6-09, GAP-6-15, U7).
- Les services connectés passent par le **port `ConnectedService`** (ADR-0041, EF-SRC-033) : catégories, file côté serveur (ticket `download_request` : demander, interroger, récupérer, annuler), revendication et purge, relevé et restauration des réglages amont, sonde, mode d'authentification, réveil. Les règles propres à Suwayomi décrites ici (exclusion du téléchargement automatique, état « téléchargé », `detach --restore-upstream`, `ui_login`) deviennent l'implémentation Suwayomi de ces capacités ; lncrawl (M2) réutilise `server_queue` sans nouveau cas particulier.
- **Suwayomi après #2512 et #2513** (U7) : l'adaptateur n'appelle que les routes REST conservées (`chapter/{id}/download` en GET et HEAD, route de page, vignettes), vérifié par une liste blanche testée ; toutes les requêtes GraphQL partent en `application/json` ; un 401 « CSRF mismatch » est classé `failure_class = version`, jamais `auth` (pas de demande d'identifiants).
- Capacité Source `ranged_content` et drapeau « tome clos » (EF-SRC-032) : un service qui livre un fichier pour une plage de chapitres (lncrawl) le déclare ; la couverture est enregistrée dans `unit_coverage` (ADR-0040).

## Exigences liées
EF-SRC-006, EF-SRC-008, EF-SRC-009, EF-SRC-010, EF-SRC-021, EF-INT-008, EF-INT-022, ENF-COMP-001, ENF-OPS-005, ENF-LEG-001, EF-SRC-025, EF-SRC-026, EF-SRC-027, EF-SRC-028, EF-BIB-035, EF-SRC-029, EF-BIB-042, EF-SRC-031, EF-SRC-026, EF-EXP-053, EF-SRC-032, EF-SRC-033.

## Sources
- https://github.com/Suwayomi/Suwayomi-Server
- https://github.com/keiyoushi/extensions-source
- https://github.com/lncrawl/lightnovel-crawler
- https://github.com/oae/kaizoku
- https://github.com/maxpiva/Rensaio
- https://github.com/opds-community/drafts
- https://github.com/Suwayomi/Suwayomi-Server/issues/2353
- https://torznab.github.io/spec-1.3-draft/torznab/Specification-v1.3.html
- https://github.com/Suwayomi/Suwayomi-Server/issues/1004
- `deliverables/review/explorateur-passe3.md` (P2, P3), `deliverables/review/ARBITRAGES-passe3.md` (C5, C15, C16)
- `deliverables/review/chasseur-trous-passe4.md` (GAP-4-04, GAP-4-05, GAP-4-06)
- `deliverables/review/explorateur-passe4.md` (R7)
- https://www.github.com/Suwayomi/Suwayomi-Server/releases
- `deliverables/review/ARBITRAGES-passe4.md`
- https://github.com/Suwayomi/Suwayomi-Server/pull/623
- `deliverables/review/chasseur-trous-passe5.md`
- `deliverables/review/explorateur-passe5.md`
- `deliverables/review/ARBITRAGES-passe5.md`

## Amendement spike S5 (2026-11-04)
Constats contre Suwayomi v2.4.2366 (détail : `spikes/s5/RESULTATS.md`). La route `GET /api/v1/chapter/{id}/download` renvoie bien un CBZ (`application/vnd.comicbook+zip`, signature `PK`) : le mode CBZ de la V1 est confirmé et le mode pages reste hors périmètre. Précisions : (1) GraphQL ne sait pas renvoyer l'archive, seule la route REST la sert (risque R7 maintenu : pas de repli GraphQL) ; (2) le téléchargeur de Suwayomi ignore la source locale, donc le banc de contrat simule le téléchargement en posant le CBZ synthétique et ne couvre pas la production du CBZ par une source distante ; (3) les dépôts d'extensions s'appellent `extensionStores` ; (4) `ui_login` v2.4 est mono-compte (JWT, `login` / `refreshToken`), les comptes multiples et leurs permissions ne sont pas testables.
