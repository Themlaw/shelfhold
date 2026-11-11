# Journal des modifications

Format : [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/). Versionnement : [SemVer](https://semver.org/lang/fr/). Procédure de release : `docs/releasing.md`.

## [0.3.0] - 2026-11-11 — M1b

Migration : oui (migrations 0013 à 0017, voir ci-dessous), réversible : non automatiquement. `shelfhold migrate` sauvegarde la base avant de migrer (`/data/backups/pre-migration`) ; un retour à la 0.2.0 passe par cette sauvegarde et par `shelfhold reconcile --adopt-after-restore` avec le service complet (`docs/admin/retour-arriere.md`). Version minimale de Kavita prise en charge : **0.9.1** (ENF-COMP-001) ; les étagères famille la refusent en dessous.

Migrations :

- 0013 : passkeys (`webauthn_credential`), exécutions de sauvegarde (`backup_run`), empreinte d'instance d'un service connecté (`source_config`).
- 0014 : `download_request.visibility` et `requester_user_id` (qui a demandé, qui peut voir).
- 0015 : RP ID de chaque passkey et passkey à l'origine d'une session.
- 0016 : `follow_request` (demandes de suivi approuvées, une par série amont).
- 0017 : `safeguard_backlog` (ce que « Mettre à l'abri » n'a pas pu copier à son dernier passage).

### Ajouté

- **Rôles, droits et passkeys** (EF-USR-005, EF-USR-016, EF-USR-022, EF-USR-024, EF-USR-025, ENF-SEC-010) : table de vérité exhaustive action x rôle x crédential avec colonne de fraîcheur (`shelfhold access-table`), droit « classer », visibilité de l'activité (un adulte compte comme parent), démasquage audité. Passkeys WebAuthn : connexion sans mot de passe, ré-authentification en un geste, révocation, avertissement si le domaine change ; HTTPS ou `localhost` seulement.
- **Contrôle parental** (EF-BIB-036, EF-BIB-037, EF-BIB-055, EF-EXP-002, EF-EXP-046, EF-EXP-047, EF-EXP-048, EF-EXP-050, EF-EXP-055, EF-USR-015) : étagères famille (plafond sous 18+) sur une bibliothèque Kavita dédiée, retenue des Œuvres non classées, âge effectif (maximum des Éditions, âge d'Édition explicite), `shelfhold rate`, écran « À classer » et vue « À ranger », retrait par plan (`shelfhold shelf withdraw`, jamais automatique), adoption d'une bibliothèque lue par des enfants, âge poussé vers Kavita sur la série et verrouillé (EF-EXP-040).
- **Suwayomi** (EF-SRC-009, EF-SRC-025, EF-SRC-026, EF-SRC-029, EF-SRC-031, EF-SRC-033, EF-SRC-036) : `shelfhold follow` et `unfollow`, catégorie « Shelfhold » exclue du téléchargement automatique, compte de service en `ui_login` (jetons JWT en mémoire, droits minimaux vérifiés), détection d'une réinstallation (`source reconnect`), mode piloté, sonde fonctionnelle (source non chargée) et relance bornée, `series.stale` agrégé, refus guidé quand `excludeUnreadChapters` ou `excludeNotStarted` sont actifs.
- **Sablier** (EF-INT-022, ENF-OPS-011) : port `WakeHook`, réveil de Suwayomi et de Kavita à la demande, sans jamais compter un réveil raté contre une source ni lever la barrière parentale.
- **Copie conservée et « Mettre à l'abri »** (EF-BIB-006, EF-BIB-027, EF-BIB-031, EF-BIB-038, EF-BIB-042, EF-BIB-048) : copie indépendante dans `/store` (clone sinon copie vérifiée, jamais de lien), dérivé canonique déclaré, `store/INDEX.jsonl`, copie avant la disparition de la source, quota et arriéré, purge chez Suwayomi facultative (`shelfhold shelf purge-upstream`).
- **Racines** (EF-EXP-043, EF-EXP-064) : `shelfhold root takeover|reinit|restored`, `store reinit`, `reconcile --rebaseline-inodes` ; la sentinelle est authentifiée et ne recule jamais.
- **Pierres tombales** (EF-BIB-052, EF-BIB-053, EF-BIB-054) : journal chaîné, détection d'une troncature (`tombstone set-aside-journal`), `tombstone rekey`, `tombstone flush`, `secrets import` ; purge étendue à `/store`, à son index, au téléchargement Suwayomi, au désabonnement, aux tâches et notifications, avec `purge --checkpoint`, `--leftovers` et `--kept-ack`.
- **Sauvegarde intégrée** (EF-INT-014, EF-INT-015, EF-INT-027, ENF-OPS-001) : `shelfhold backup run|verify|restore`, chaque nuit, archive entièrement chiffrée (age, scrypt à facteur fixe) dans un dossier distinct, rétention, alerte d'échec ; restauration vers un autre dossier qui révoque sessions et clés d'API ; `shelfhold restore acknowledge` ; compteur d'identité hors base.
- **Exploitation** (EF-INT-017, ENF-OPS-009, ENF-OPS-014, ENF-OPS-016, ENF-PERF-001, ENF-PERF-008, ENF-PERF-009) : fenêtre de maintenance réglable, classes de priorité avec vieillissement, rétention technique (événements 90 jours, audit 1 an), entretien SQLite (`optimize`, point de contrôle, `VACUUM` toutes les 28 jours), bancs de mémoire et de fenêtre sur un profil de référence (135 Mo au repos).
- **Plugins tiers** (EF-SRC-019, ENF-SEC-009) : `shelfhold plugin install|enable|disable|remove|list` ; désactivés par défaut, chargés seulement après une variable d'environnement et une phrase tapée liée aux fichiers installés.
- **Catalogue** (EF-BIB-025, EF-BIB-029, EF-BIB-033, EF-BIB-039, EF-EXP-062) : Série facultative au-dessus de l'Œuvre, rattachement tardif, relations d'Œuvres, `rights` dans le ComicInfo, `shelfhold relink`.
- **Chaîne de livraison** (ENF-SEC-007, ENF-QUAL-007) : SBOM SPDX et attestations de provenance sur chaque plateforme de l'image, analyse Grype, CodeQL, preuves de release en assets, nettoyage planifié des images de PR, outils téléchargés par la CI en cache vérifié par somme de contrôle.
- **Qualité** (ENF-QUAL-006, ENF-OPS-008) : suite de propriétés sur le classement, parcours de bout en bout sur l'image réelle (famille, mise à l'abri, Sablier, sauvegarde et restauration, compte de service, passkey, retour arrière vers la 0.2.0), additions de nuit contre de vrais Kavita et Suwayomi.
- **Documentation d'administration** : `docs/admin/` complété (rôles, famille, « À classer », Suwayomi multi-comptes, purge, copie conservée, retour arrière, restauration), `docs/decisions/M1b.md`.
- Variables : `SHELFHOLD_BACKUP_DIR`, `SHELFHOLD_BACKUP_PASSPHRASE_FILE`, `SHELFHOLD_BACKUP_RETENTION`, `SHELFHOLD_BACKUP_STALE_HOURS`, `SHELFHOLD_WAKE_URL`, `_WAKE_SUWAYOMI`, `_WAKE_KAVITA`, `_WAKE_TIMEOUT_S`, `_WAKE_SESSION_S`, `_WAKE_TOKEN`, `SHELFHOLD_MAINTENANCE_WINDOW_START`, `_END`, `SHELFHOLD_TIMEZONE`, `SHELFHOLD_RETENTION_EVENTS_DAYS`, `SHELFHOLD_RETENTION_AUDIT_DAYS` (`docs/reference/config.md`).

### Modifié

- **`shelfhold shelf create`** accepte tous les plafonds de l'échelle (`0`, `10+`, `12+`, `13+`, `16+`, `18+`, `X`) ; sous 18+, l'étagère est une étagère famille.
- **Restauration** : toutes les sessions et clés d'API sont révoquées ; la connexion par passkey est refusée tant que `restore acknowledge` n'a pas été fait.
- **Gel de restauration** : un seul état (`FreezeState`) pour les écritures, les tâches et les services.
- **`source.check_mode`** vaut `always_on`, ou `piloted` quand un réveil est configuré.
- Le réglage `shelf.upstream_purge` du plan est `shelf.purge_upstream_after_export` (désactivé par défaut).
- Code réservé aux tests retiré de `src` (environ 255 lignes) ; les méthodes d'`ExportTarget` propres à Kavita sont dépréciées (SDK 1.1).

### Corrigé

- `shelfhold jobs run` d'une tâche à fenêtre ne s'exécutait jamais (la fenêtre stockée était celle de la nuit suivante).
- Une purge ne laisse plus de titre dans les pages libres de la base ni dans son journal (`secure_delete`, point de contrôle).
- Rejeu du journal des pierres tombales après une restauration, un remontage ou une sortie de racine : l'index suit sans redémarrage.
- `rate` d'une Œuvre ne règle plus les suggestions d'âge plus élevées d'une Édition (migration de données 0025 : les suggestions déjà réglées sont rouvertes).
- Course entre le renouvellement du bail et la vérification de la session d'écriture.

### Sécurité

- Les actions réservées sont refusées à toute clé d'API et à tout jeton, portée `admin` comprise ; la table complète est publiée par `shelfhold access-table`.
- Une restauration ne rend plus valides une session fermée, une clé révoquée ni un mot de passe réinitialisé depuis la sauvegarde.
- Les plugins tiers ne peuvent plus être chargés sans l'accord explicite et vérifié de l'administrateur.

### Connu

- **Le second registre d'images et le miroir du dépôt sont à venir** (ENF-GOV-004, L28) : en attente du jeton du second registre. La 0.3.0 est publiée sur GHCR seulement ; ENF-GOV-004 est la seule exigence de M1b sans test.
- Sur le Suwayomi épinglé, `excludeUnreadChapters` et `excludeNotStarted` sont activés par défaut : `source add` est refusé tant qu'ils ne sont pas désactivés (`docs/admin/suwayomi-multi-comptes.md`).
- Sans passkey ni mot de passe récent hors profil durci, une session volée peut enregistrer une passkey (`docs/decisions/M1b.md`, point 1 des points à valider).
- Une étagère en mode conservation créée par `POST /api/v1/shelves` n'est pas liée à une bibliothèque Kavita : la lier par la CLI.
- Certains chemins du journal structuré (`fsops.write_atomic`, `export.op_done`) contiennent encore le titre.
- Les migrations 0020 et suivantes (M2) sont déjà sur `main` ; elles ne font pas partie des exigences de M1b.

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
