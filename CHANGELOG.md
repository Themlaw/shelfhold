# Journal des modifications

Format : [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/). Versionnement : [SemVer](https://semver.org/lang/fr/). Procédure de release : `docs/releasing.md`.

## [0.2.0] - non publiée — M1a

Migration : oui (migrations 0002 à 0010, voir ci-dessous), réversible : non automatiquement. `shelfhold migrate` sauvegarde la base avant de migrer (`/data/backups/pre-migration`) ; un retour à 0.1 passe par la restauration de cette sauvegarde (`docs/admin/restauration.md`).

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

### Ajouté

- Pont Suwayomi vers Kavita : service connecté Suwayomi (GraphQL, CBZ, authentification Basic, sous-sources), suivi « toujours actif » avec tickets de téléchargement, classes d'échec et fraîcheur par série, politique d'équipe, filigrane de suivi, rattrapage et demandes par plage.
- Export vers Kavita : client à routes en liste blanche et plage de versions, clé nommée à expiration, nommage Kavita, ComicInfo 2.0 (brouillon 2.1) et empaquetage CBZ, planificateur avec aperçu et application, manifeste, séries exportées, miroir de provenance par racine (format 1.2), réinstallation de Kavita détectée, arrivée par scrutation, propriété des champs et audit d'accès.
- Modèle de catalogue : Œuvres, Éditions, Unités, Variantes et équipes, numérotation versionnée, BCP 47, dates partielles.
- Stockage : mode passage, `unit.json`, `content_hash`, registre des exemplaires et copies indépendantes ; racines physiques avec sentinelle, écritures atomiques et bail par racine ; journal d'intentions, verrous de ressource, plans versionnés (412) et instance unique.
- Adoption d'une bibliothèque Kavita adulte existante sans réécriture (`shelfhold adopt --dry-run`, `--confirm`), appariement avec le suivi Suwayomi, détection de la bascule depuis des scripts, sonde d'écriture avec la correction exacte, détection d'un autre écrivain et rapprochement réduit, rapatriement avant démontage et `detach --restore-upstream` (services livrés ; câblage dans la PR de câblage).
- Effacement : `purge --now` avec portées, pierres tombales chiffrées (journal 2.0, registre des `kid`, index d'appartenance), lignes en attente par racine.
- Restauration : époque de restauration, état `restored_behind` en lecture seule, `shelfhold reconcile --adopt-after-restore`.
- Exploitation : garde disque avec hystérésis, refus de `/data` sur stockage réseau, arrêt propre, matrice de pannes avec simulateur, états d'attente et commandes de sortie (`shelfhold status`, routes `/api/v1/status` et d'exploitation), notifications Apprise, planificateur Huey avec fenêtre de maintenance, assets de release `schema.sql` et dictionnaire de données, `shelfhold stats` local sans télémétrie.
- Configuration sans interface : `config get|set|apply`, `target add`, `source add`, `shelf create`, CLI locale par socket Unix avec `SO_PEERCRED`.
- Authentification locale : administrateur, sessions serveur, CSRF, tentatives limitées, clés d'API hachées, actions réservées avec fraîcheur (`reauth-required`), profil durci facultatif.
- Documentation d'administration : installation sans interface, adoption, migration depuis des scripts, rapatriement, retour arrière, états d'attente, restauration, purge, garde disque, redéploiement, racines physiques, montage `adopt_ro`, exposition publique, budget mémoire et disque, décisions d'exécution de M1a.
- Conventions d'API : problèmes RFC 9457 avec catalogue d'erreurs stable, pagination par curseur, idempotence, `If-Match`, réponses 202.

### Modifié

- L'image du compose d'exemple passe à l'étiquette `0.2`.
- La session d'administration dure 30 jours glissants par défaut ; 30 minutes d'inactivité et 12 heures au plus dans le profil durci.
- `/healthz` ne montre plus la version aux anonymes ; `/openapi.json` n'est plus public (`GET /api/v1/openapi.json`, authentifié).
- CI : tests découpés en tranches par machine, bancs coupables dans un job à part.

### Sécurité

- En-têtes de sécurité globaux sur toutes les réponses.
- Garde SSRF du Fetcher avec cibles du réseau local épinglées ; toute adresse publique refusée pour une destination.
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
