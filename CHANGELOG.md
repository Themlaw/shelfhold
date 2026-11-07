# Journal des modifications

Format : [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/). Versionnement : [SemVer](https://semver.org/lang/fr/). Procédure de release : `docs/releasing.md`.

## [0.2.0] - 2026-11-07 — M1a

Migration : oui (migrations 0002 à 0012, voir ci-dessous), réversible : non automatiquement. `shelfhold migrate` sauvegarde la base avant de migrer (`/data/backups/pre-migration`) ; un retour à 0.1 passe par la restauration de cette sauvegarde (`docs/admin/restauration.md`).

Migrations :

- 0002 : plateforme, identité et garanties de cohérence.
- 0003 : catalogue et acquisition.
- 0004 : stockage, export et lecture (éditions rattachées aux étagères, mode des racines physiques).
- 0005 : `job_view.window_end` et `cursor` (tâches de fenêtre de maintenance).
- 0006 : opération `write` du journal d'intentions ; version de plan en condensé hexadécimal.
- 0007 : un seul ticket de téléchargement ouvert par Variante.
- 0008 : opération `register` pour les destinations en tirage.
- 0009 : `tombstone_lost_kid` (clés de pierres tombales abandonnées).
- 0010 : `export_series.paused_reason` (série en pause par un autre écrivain).
- 0011 : `follow_notice` (ce que le suivi a déjà dit ou attend de l'administrateur, pour ne pas le redire après un redémarrage) et une seule liaison active par série amont (index unique partiel ; sur une base qui en aurait deux, la plus ancienne reste active).
- 0012 : normaliseurs versionnés de la langue d'Édition (`lang@1`) et de l'identifiant amont (`remote_unit_id@1`), et `export_record.resolved_at` (heure d'arrivée dans Kavita).

### Ajouté

- Journaux détaillés à plusieurs niveaux (`docs/admin/journaux.md`) : début et fin de chaque job avec durée et compteurs, décisions du suivi, de l'export, du rapprochement et de la purge, appels à Kavita et à Suwayomi (route sans query string), écritures de fichiers ; contexte lié (`job_id`, `series_id`, `edition_id`, `unit_id`, `target_id`) ; aucun secret ni nom de fichier d'archive non fiable au-dessus de `DEBUG`.
- Pont Suwayomi vers Kavita : service connecté Suwayomi (GraphQL, CBZ, authentification Basic, sous-sources), suivi « toujours actif » avec tickets de téléchargement, classes d'échec et fraîcheur par série, politique d'équipe, filigrane de suivi, rattrapage et demandes par plage.
- Export vers Kavita : client à routes en liste blanche et plage de versions, clé nommée à expiration, nommage Kavita, ComicInfo 2.0 (brouillon 2.1) et empaquetage CBZ, planificateur avec aperçu et application, manifeste, séries exportées, miroir de provenance par racine (format 1.2), réinstallation de Kavita détectée, arrivée par scrutation, propriété des champs et audit d'accès.
- Modèle de catalogue : Œuvres, Éditions, Unités, Variantes et équipes, numérotation versionnée, BCP 47, dates partielles.
- Stockage : mode passage, `unit.json`, `content_hash`, registre des exemplaires et copies indépendantes ; racines physiques avec sentinelle, écritures atomiques et bail par racine ; journal d'intentions, verrous de ressource, plans versionnés (412) et instance unique.
- Adoption d'une bibliothèque Kavita adulte existante sans réécriture (`shelfhold adopt --dry-run`, `--confirm`), appariement avec le suivi Suwayomi, détection de la bascule depuis des scripts, sonde d'écriture avec la correction exacte, détection d'un autre écrivain et rapprochement réduit, rapatriement avant démontage (`adopt --repatriate`) et `detach --restore-upstream`.
- Effacement : `purge --now` avec portées, pierres tombales chiffrées (journal 2.0, registre des `kid`, index d'appartenance), lignes en attente par racine.
- Restauration : époque de restauration, état `restored_behind` en lecture seule, `shelfhold reconcile --adopt-after-restore`.
- API : `POST /api/v1/plans/{id}/apply` répond `202` et `Location: /api/v1/jobs/{id}` (tâche `export.apply`, qui relit les noms à Kavita avant d'écrire), `GET /api/v1/jobs/{id}`, `GET /api/v1/intents`, `POST /api/v1/intents/{id}/resolve` (action réservée `intents.resolve`), `GET|POST /api/v1/editions/{id}/backfill`.
- CLI : `shelfhold intents list|resolve` (règle d'une intention ouverte, ou renoncement saisi deux fois) et `shelfhold backfill --gaps|--all` (estimation, confirmation au-delà de 100 chapitres ou 5 Gio).
- `shelfhold normalize migrate <normaliseur>` (aperçu, puis `--confirm <empreinte>`) : migration explicite d'un normaliseur versionné, qui consulte les pierres tombales (`tombstone.migration_hit`) et ne fusionne rien ; tant qu'aucun normaliseur plus récent n'existe dans le code, il répond « rien à migrer » ; aucun chargement externe. Action réservée `normalize.migrate`.
- Restauration : la suppression que le rejeu du journal propose pour un chapitre irremplaçable exige de retaper son identifiant (`reconcile --adopt-after-restore --confirm-deletion --confirm-unit <identifiant>`).
- Un CBZ dont une entrée n'est pas une image de la liste blanche (SVG, HTML, extension mensongère) est écarté : jamais redemandé, `content.untrusted_entry` émis, aucun échec de contenu compté.
- Exploitation : garde disque avec hystérésis, refus de `/data` sur stockage réseau, arrêt propre, matrice de pannes avec simulateur, états d'attente et commandes de sortie (`shelfhold status`, routes `/api/v1/status` et d'exploitation), notifications Apprise, planificateur Huey avec fenêtre de maintenance, assets de release `schema.sql` et dictionnaire de données, `shelfhold stats` local sans télémétrie.
- Câblage de production : racine de composition partagée par le serveur, la CLI et les tâches ; consommateurs Huey embarqués ; tâches `follow.cycle`, `export.run`, `reconcile.cycle`, `reconcile.hash`, audit d'accès quotidien et `notify.dispatch` ; canaux Apprise sur le bus d'exploitation de tous les éditeurs ; commandes `adopt pair|--pair-auto|set-follow-from|--confirm-migration|--repatriate`, `detach --restore-upstream`, `purge --now` et `probe` avec leurs routes ; enregistrement de l'instance et des racines physiques ; lien de l'étagère avec une bibliothèque Kavita (`shelf create`). Code d'erreur `purge.collateral`.
- Découverte des séries de la bibliothèque Suwayomi (#67, EF-SRC-009) : au début de chaque cycle, chaque série suivie par Suwayomi qui n'a pas de liaison reçoit son Œuvre, son Édition sur l'étagère adulte et sa liaison. Le premier cycle ne demande rien (seuls les chapitres publiés après lui sont récupérés) ; une série sous pierre tombale n'est jamais créée ; une série adoptée en attente d'appariement est laissée à `adopt pair`. Sans étagère adulte, l'événement `series.awaiting_shelf` dit la commande à lancer (`shelf create`).
- Tests de bout en bout : parcours d'installation neuve jusqu'au premier chapitre, redéploiement sans perte et détachement sur l'image, contre des Kavita et Suwayomi simulés ; test de nuit contre de vrais Kavita et Suwayomi, avec une extension Suwayomi de test construite par le dépôt (dépendances vérifiées par somme de contrôle).
- Remédiation de l'audit (L45 à L59) : `SHELFHOLD_UMASK` (défaut `0022`) appliqué aux fichiers de la bibliothèque (EF-EXP-006) ; contributeurs et dates partielles de l'ingestion jusqu'au ComicInfo (L49) ; couverture de série envoyée à Kavita (EF-EXP-045) ; `adopt set-coverage` et couverture `Vol.N`, `adoption_refused` et `coverage_unknown` persistés (L54) ; contrôle vérifié du rapatriement par l'API de Kavita, projection `work.json`, bail versionné, `shelfhold meta rebuild|check` du miroir de provenance (L56) ; golden CBZ, invariants I24 à I42, bornes de couverture, documentation d'exposition publique et de sauvegarde (L57) ; `docs/reference/config.md` généré (EF-INT-024).
- Contrôles de qualité : garde de joignabilité des symboles cités par les tests `req` (liste d'autorisations `tools/production-reachability-allowlist.toml`), diff OpenAPI complet, migrations publiées figées par empreinte (`tools/check_patch_migrations.py`), contrats import-linter en erreur (L58).
- Contrôle de release : une version corrective (X.Y.Z, Z > 0) qui ajouterait une migration, ou modifierait une migration publiée, est refusée par `release.yml` (ENF-OPS-002).
- Configuration sans interface : `config get|set|apply`, `target add`, `source add`, `shelf create`, CLI locale par socket Unix avec `SO_PEERCRED`.
- Authentification locale : administrateur, sessions serveur, CSRF, tentatives limitées, clés d'API hachées, actions réservées avec fraîcheur (`reauth-required`), profil durci facultatif.
- Documentation d'administration : installation sans interface, adoption, migration depuis des scripts, rapatriement, retour arrière, états d'attente, restauration, purge, garde disque, redéploiement, racines physiques, montage `adopt_ro`, exposition publique, budget mémoire et disque, décisions d'exécution de M1a.
- Fermeture du jalon (L60) : `shelfhold edition set-series-name <édition> "<nom>"` (route `POST /api/v1/editions/{id}/series-name`) fixe le nom exporté d'une série dont le nom est déjà pris, soit par une autre série exportée de la même bibliothèque Kavita, soit par une série que Kavita montre et qui vient d'une autre source (d'après la dernière lecture de la liste des séries de la bibliothèque, faite par l'export ; si Kavita ne peut pas être lue, la liste précédente reste) (EF-EXP-032). Le nom est refusé s'il contient un caractère de contrôle, un caractère bidirectionnel, `/` ou `\`, s'il vaut `.`, `..` ou `.shelfhold`, ou s'il est réservé par Windows. L'état d'attente `series_name_collision` figure dans `shelfhold status` avec cette commande, comme `mount_sentinel_missing` (EF-EXP-029, sortie `shelfhold target recheck <cible>`). Les scans de Kavita (vidage différé, relance d'arrivée, contrôle de `adopt --repatriate --check`) demandent la sentinelle de la racine (`require_scan_allowed`) ; un montage `adopt_ro` dédié n'a pas de sentinelle (EF-EXP-060) : son contrôle est que le montage soit là. Les demandes par plage sont câblées dans le suivi (EF-SRC-034) et les fichiers de `/data/ranged` laissés par une transaction non validée sont balayés (plus d'une heure, sans `export_item`).
- Conventions d'API : problèmes RFC 9457 avec catalogue d'erreurs stable, pagination par curseur, idempotence, `If-Match`, réponses 202.

- Exploitation (L52) : une tâche qui échoue est réessayée (3 essais) puis devient `dead_letter` avec l'événement `job.dead_lettered` (sortie `shelfhold jobs list|retry`) ; `reconcile.drift` d'une intention non résolue au démarrage, `secret.undecryptable` d'un trousseau illisible et les événements du miroir passent par le bus d'exploitation et créent leurs notifications ; sonde d'écriture de chaque racine au démarrage (`target.permission_denied` avec la cause et le bloc compose à coller, sans arrêter le démarrage) ; le montage `adopt_ro` figure dans le compose d'exemple ; preuves du kill -9 de 1 000 tâches et du redéploiement en plein export.
- `shelfhold status` complété (L53) : avertissements du trousseau, dont la clé maîtresse dans `/data` (ENF-SEC-005) ; séries silencieuses (`series.stale`, k = 3, plancher de 14 jours, EF-SRC-026 c) ; séries purgées encore présentes dans Suwayomi (`series.tombstoned_upstream`, EF-BIB-053) ; par racine, reflink, lien dur vers `/store` (« non applicable » sans `/store`) et stabilité des inodes, sondés au démarrage sans rien laisser sur la racine (EF-EXP-059) ; âge de la liste `known_bad_versions` (ENF-COMP-001) ; `adoption_refused`, `coverage_unknown` et `restored_behind` listés comme attentes (EF-INT-023, port `AdoptionExit`). Sortie JSON : clés `stale_series`, `tombstoned_upstream`, `capabilities`, `known_bad`. Sans migration.
- Version minimale de Kavita prise en charge : **0.9.1** (ENF-COMP-001). Chaque note de version la rappelle.

### Connu

- Une série d'une sous-source multilingue (`all`) est découverte avec une Édition de langue inconnue (`x-unknown`, état `language_unknown`) : sa langue se donne par `shelfhold edition set-lang`.
- Le paquet de diagnostic n'existe pas (M3) ; `is_diagnostic_excluded` et `redact_source_urls` sont posés pour lui.
- `root takeover|reinit|restored` et `store reinit` n'existent pas (M1b) : une sentinelle durablement absente ou différente se résout en remontant le bon volume, puis `shelfhold target recheck`. `secrets import` n'existe pas (`tombstone accept-lost-keyring` quitte le même état).
- Un service connecté qui déclare `ranged_content` voit ses fichiers de plage gardés sous `/data/ranged/` avec leur couverture ; leur export est de M2 (aucun service de M1a n'a cette capacité : Suwayomi livre un fichier par chapitre).
- Le dossier de téléchargement de Suwayomi (`settings.downloadsPath`) est un chemin de son propre conteneur, et 01 ne prévoit pas de correspondance côté source : il n'est comparé à la bibliothèque (signal de la bascule, EF-EXP-044) que s'il tombe sous une racine que Shelfhold connaît ; vide (valeur par défaut), relatif ou propre à son espace de noms, la bascule est « non vérifiable » et `shelfhold status` le dit. Il n'est lu que lors du rapport de bascule et de l'appariement.
- `detach --restore-upstream` retire la catégorie d'exclusion que Shelfhold a posée et notée (à chaque appariement ou reprise, il note celle qui porte sa marque, la catégorie « Shelfhold », apparue depuis le relevé) ; celle que vous avez ajoutée vous-même reste en place et la commande la liste. Installation d'avant ces notes : seule une catégorie nommée « Shelfhold » est retirée, toute autre est listée sans être retirée.

### Modifié

- Les racines physiques sont relues à chaque appel par `series resume`, `target recheck`, l'aperçu des plans, le constat de `restored_behind` et la garde disque : un `target add` fait pendant que le serveur tourne est connu sans redémarrage (L60).
- `follow.cycle` ne prend plus le bail de `/library` quand il n'a rien à y écrire, et un candidat bloqué par « destination occupée » n'est plus cherché sur le disque à chaque passage (la vérification de la fenêtre de maintenance l'oublie) : aucun réveil de disque à vide (ENF-OPS-014).
- Un 401 de Kavita (clé révoquée) émet `target.unreachable` et désactive la destination, comme la matrice de pannes le prévoit.
- L'image du compose d'exemple passe à l'étiquette `0.2`.
- La session d'administration dure 30 jours glissants par défaut ; 30 minutes d'inactivité et 12 heures au plus dans le profil durci.
- `/healthz` ne montre plus la version aux anonymes ; `/openapi.json` n'est plus public (`GET /api/v1/openapi.json`, authentifié).
- CI : tests découpés en tranches par machine, bancs coupables dans un job à part ; les parcours de bout en bout de l'image sont répartis en shards de moins de 10 minutes (`e2e/shards.py`), tous sur la même image ; l'étiquette OCI `version` de l'image vaut la version du paquet.

### Sécurité

- Risque administrateur accepté à l'ajout d'une destination (`--accept-admin-risk`) tracé dans le journal d'audit ; session « se souvenir de moi » durcie ; routes réservées réelles (L47). Texte amont assaini jusqu'au ComicInfo, plafonds d'archive, dates futures refusées (L45).
- En-têtes de sécurité globaux sur toutes les réponses.
- Garde SSRF du Fetcher avec cibles du réseau local épinglées ; toute adresse IP publique refusée pour une destination. La même règle vaut pour l'adresse de Suwayomi, avant toute requête (ENF-SEC-001, L60) : une IP publique est refusée (adresses IPv6 de transition qui portent une IPv4 publique comprises : 6to4, NAT64, Teredo, IPv4 mappée) ; `http://` est réservé aux noms qui ont l'air locaux (une seule étiquette, `.lan`, `.local`, `.internal`...) et qui ne se résolvent vers aucune adresse publique ; tout autre nom doit être en `https://`. Un nom local qui ne se résout pas est laissé au Fetcher, qui épingle et vérifie les adresses à chaque connexion. L'adresse enregistrée est revérifiée à chaque usage ; refusée, aucun appel n'est envoyé et `shelfhold status` la liste.
- Archives et XML non fiables ouverts par des chemins bornés (liste blanche CBZ, plafonds d'image, décodage dans un processus sans réseau), texte amont assaini.
- Secrets scellés par un trousseau avec clés retirées, rotation reprenable et export age.
- Mots de passe en argon2id, cookies `HttpOnly` et `SameSite=Strict`, en-têtes d'identité de proxy retirés, clés d'API stockées hachées et refusées pour les actions réservées.
- Pierres tombales chiffrées : aucune valeur en clair ni empreinte dans la base ou le journal.

## [0.1.0] - 2026-11-04 — M0 fondations

Migration : oui (schéma initial), réversible : oui

### Ajouté

- Squelette applicatif : monolithe modulaire Python avec contrats d'architecture vérifiés (import-linter), CLI `shelfhold migrate | serve | config show`, configuration par environnement, fichiers de secrets et réglages en base, journaux structurés, sondes `/healthz` et `/readyz`.
- Volumes `/data` et `/store` séparés ; migration initiale avec sauvegarde avant migration, restauration en cas d'échec et verrou d'exclusion.
- Image OCI durcie (utilisateur non privilégié, système de fichiers en lecture seule, épinglage par condensat) pour amd64 et arm64, publiée sur GHCR, avec compose d'exemple.
- CI proportionnée : lint, typage, tests avec seuils de couverture, jeux de fichiers synthétiques, contrôle de noms interdits, traçabilité exigences vers tests.
- Gouvernance et posture légale : licence AGPL-3.0-or-later avec exception de plugin provisoire, SDK sous MIT, DCO, politique d'IA, CRA, portes de décision, registre de pré-mortem, revue de chevauchement.
- Spikes S1 (file de jobs), S2 (front), S4 (RAM et taille d'image) et S5 (contrat Suwayomi) tranchés ; résultats dans `spikes/*/RESULTATS.md`.
- Relevé de départ documentaire générique (`docs/recherche/releve-depart.md`) ; entretiens utilisateurs reportés.
