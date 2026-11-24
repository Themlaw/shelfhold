---
status: "proposée (en attente du porteur : D-P1 à confirmer ; D-P2, D-P3, D-P4 et D-P6 ouvertes)"
date: 2026-11-20
decision-makers: porteur (D-P1 à D-P6), mainteneur (D10 à D16, D24)
---
# ADR-0056 — Plugins tiers isolés (compagnon `plugin-runner`) et index signé

> **Décisions du porteur, aucune n'est tranchée** : D-P1 (forme de l'isolation), D-P2 (publication de `shelfhold-sdk`), D-P3 (index officiel et clé), D-P4 (relecture de l'exception de plugin, Q17) et D-P6 (premier tracker) sont des **recommandations, en attente du porteur** ; pour D-P5 (sync Kobo, Q21), seul le **critère** est écrit, la question posée au porteur reste ouverte. **Décisions déléguées** (recommandations du plan retenues par le mainteneur) : D10 (nom d'import), D11 (`api` 2 et shim), D12 (`rpc_protocol` 1), D13 (mode par défaut), D14 (index), D15 (cycle de vie), D16 (ABI), D24 (numéros de version). D25 (entrées d'allowlist `until = "trigger"`) et D26 (trois formats de position) ne relèvent pas de cette ADR : elles sont écrites par leurs lots. Amende ADR-0007, ADR-0014, ADR-0016, ADR-0024 et ADR-0036 ; corrige 01, 02, 03, 04 et 06. Le statut passe à « acceptée » quand le porteur confirme D-P1 ; les autres décisions du porteur s'y ajoutent par amendement.

## Contexte et problème
M6 ouvre Shelfhold aux plugins tiers (EF-SRC-016, EF-SRC-017, EF-SRC-019, EF-SRC-020, EF-SRC-030). ADR-0007 reporte l'isolation « en M6 » sans la décrire ; ADR-0024 fige le contrat de données `api` 1 et la compatibilité N-1. La garde de M1b (`plugin_host/install.py`) installe déjà une roue locale en `--only-binary :all: --require-hashes --no-deps --no-index` dans un `site-packages` séparé, mais le plugin tourne **en processus**, avec les droits du cœur, derrière `SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS` et une confirmation tapée.

L'image n'a qu'un utilisateur (`uid 1000`). Un processus non privilégié ne peut ni changer d'uid, ni créer un espace de noms réseau ou de montage, ni retirer `/data` de sa propre vue : **aucune isolation sérieuse ne peut être lancée par le cœur depuis son propre conteneur**. Avant tout code, il faut décider : la forme de l'isolation, le protocole RPC, le format et la signature de l'index, l'épinglage, le cycle de vie, l'ABI, et les versions publiées.

Contraintes déjà actées : aucun sous-processus de plugin ne voit `/data` ni le socket de la CLI locale, et aucun ne partage l'uid du cœur (passe 13, GAP-13-07, EF-USR-024) ; installation par la garde de M1b (ENF-SEC-009) ; plugins tiers désactivés par défaut (EF-SRC-019) ; aucun secret transmis à un plugin (EF-EXP-001, ENF-SEC-013) ; une signature ne vaut jamais confiance (EF-EXP-065, fiche de confiance) ; un index, quel qu'il soit, ne liste aucun module de retrait de mesure technique de protection (ADR-0015, ENF-LEG-001).

## Facteurs de décision
- Les garanties d'isolation doivent être **tenues par le moteur de conteneurs**, pas par la bonne volonté du code qui tourne à côté du plugin, puis **vérifiées** au démarrage.
- **Le compagnon est hostile** : un plugin qui s'échappe de son sous-processus a les droits du compagnon. Le cœur ne fait donc confiance à rien de ce qui vient du compagnon.
- Un seul sens de connexion : le cœur se connecte, le compagnon répond ; le plugin n'initie rien hors d'un appel de l'hôte.
- Le transport (`rpc_protocol`) est versionné séparément du contrat de données (`api`) et n'a aucun code commun avec le socket CLI hormis l'encodage JSON.
- L'index est signé, monotone, borné dans le temps, révocable, avec un sha256 par roue ; aucun dépôt préconfiguré.
- Le porteur garde la main sur les actions extérieures et irréversibles (compose, PyPI, clé publique annoncée, risque légal).

## Options considérées
### Isolation (D-P1)
#### A. Conteneur compagnon `plugin-runner` (recommandation, en attente du porteur)
Même image, commande `shelfhold plugin-runner`, service distinct du compose.
- Avantages : la seule option où Docker garantit les trois propriétés de la passe 13 (uid distinct, `/data` et socket invisibles, aucun réseau) sans privilège pour le cœur ; vérifiable par auto-contrôle ; un plantage ne touche pas le cœur.
- Inconvénients : action du porteur dans son compose ; un service de plus ; un protocole RPC à versionner.
#### B. Un seul conteneur démarré en `root`, abaissé par `setpriv` (cœur 1000, lanceur 1001)
- Avantages : rien à changer dans le compose.
- Inconvénients : l'image démarre en `root` ; le réseau ne peut pas être coupé sans privilège (`CAP_NET_ADMIN` ou espaces de noms) ; `/data` reste monté dans le même conteneur. **Refusée.**
#### C. Sous-processus lancé par le cœur dans son conteneur
- Avantages : le plus simple.
- Inconvénients : même uid que le cœur, `/data`, le socket CLI et le réseau visibles ; contredit la passe 13. **Refusée.**
#### D. WASM (Extism)
- Avantages : vrai bac à sable.
- Inconvénients : PDK Python quasi dormant (déjà écarté par ADR-0007) ; pas de parseur HTML embarqué. **Refusée.**

### Index (D14)
A. **Document JSON signé au format minisign** (retenue) ; B. PyPI seul (écartée : ni révocation, ni expiration, ni rotation, pas d'épinglage du signataire) ; C. fédération d'index (plus tard, hors M6).

## Décision

### D-P1 — Forme de l'isolation (recommandation, en attente du porteur)
Recommandation : **option A**. Le service `plugin-runner` du compose a : `user: "1001:1001"`, `network_mode: none`, `read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `pids_limit`, un `tmpfs` borné (`/tmp`, `noexec,nosuid,nodev`) ; il ne monte **ni `/data`, ni `/store`, ni `/library`, ni le socket Docker** ; il monte seulement le volume `plugins` (roues installées, **lecture seule**) et le volume `plugin-rpc` (un seul socket). Le cœur ne monte jamais le socket Docker et ne démarre pas le compagnon : c'est le compose qui le fait. Étapes du porteur fournies par L05 et L26 ; service commenté par défaut dans `compose.simple.yaml`, actif dans `e2e/compose.yaml`.

*Repli* (sans réponse ou sans compagnon) : **aucun plugin tiers isolé ne démarre**, l'UI et la CLI disent pourquoi (EF-SRC-019) ; jamais de repli silencieux vers le mode en processus ; la garde de M1b reste la seule voie, désactivée par défaut ; EF-SRC-016 est prouvée en CI par le compose d'e2e.

```
cœur (uid 1000) ──connect──▶ plugin-rpc/runner.sock ◀──listen── plugin-runner (uid 1001) ──stdio──▶ sous-processus de plugin (uid 1001)
```

### Propriétés garanties et auto-contrôle au démarrage (D-P1, L05)
Le compagnon **refuse de démarrer** (code de sortie non nul, raison écrite) si l'un de ces contrôles échoue :
- son uid est 0 ou égal à l'uid du cœur (lu dans la configuration, puis confirmé par `SO_PEERCRED` à chaque connexion) ;
- `/data`, `/store`, `/library` ou `/data/shelfhold.sock` existent ou sont lisibles ;
- une interface réseau autre que `lo` existe, ou une connexion sortante vers une adresse de documentation (RFC 5737) n'échoue pas immédiatement ;
- le système de fichiers racine est inscriptible, ou le volume `plugins` est inscriptible ;
- l'ensemble des capacités effectives n'est pas vide, ou `NoNewPrivs` vaut 0.

Côté cœur, **indépendamment de ce que dit le compagnon** : à chaque connexion, le cœur lit `SO_PEERCRED` du socket et refuse un pair dont l'uid n'est pas celui du compagnon, 1001 (`connect_runner(runner_uid=…)`, relecture de clôture S8 ; cela exclut aussi 0 et l'uid du cœur) ; le cœur n'écoute **jamais** dans le volume `plugin-rpc` (`test_core_never_listens_on_the_runner_volume`). Le compagnon, lui, refuse tout pair dont l'uid n'est pas celui du cœur.

Chaque sous-processus de plugin : environnement vidé (aucune variable `SHELFHOLD_*` hors d'une liste fermée, aucun secret), arguments sans secret, `umask 077`, `RLIMIT_CORE = 0`, plafonds `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_NPROC` (512, comparé à tous les processus de l'uid : valable dans le conteneur du compagnon, réglable par `RunnerConfig.max_tasks`), `RLIMIT_FSIZE` (256 Mio), `PR_SET_DUMPABLE = 0`, nouvelle session, descripteurs fermés hors stdio, répertoire de travail propre au plugin (`0700`) dans le `tmpfs`. Le kit gagne le contrôle « le sous-processus ne voit ni `/data` ni le socket » (`test_plugin_subprocess_never_sees_data_socket`), joué à chaque passage du kit en mode isolé.

**Confinement entre plugins (relecture de clôture S5, S7, S9).** Landlock exige l'**ABI 3** (Linux 6.2) : avant, `truncate` et `utime` ne sont pas refusables hors du dossier de travail ; refuser `truncate` dans seccomp casserait `ftruncate` sur les fichiers propres du plugin (SQLite, reprise de téléchargement), l'exigence de noyau est le moindre prix et l'auto-contrôle la vérifie. Landlock gère `READ_FILE` et `READ_DIR` : un plugin ne lit que le système (`/usr`, `/lib*`, `/etc/ssl`, fuseau horaire), l'interpréteur et son environnement, `/plugins/site-packages`, son dossier de travail, `/dev/null` et `/dev/urandom`, jamais le dossier de travail d'un autre plugin. Un plugin n'envoie de signal ni au compagnon ni à un autre plugin : `scoped = SIGNAL | ABSTRACT_UNIX_SOCKET` avec l'ABI 6 (Linux 6.12), et sinon un filtre seccomp qui refuse `kill`, `tkill`, `tgkill`, `rt_sigqueueinfo`, `rt_tgsigqueueinfo` et `pidfd_send_signal` vers tout processus autre que le plugin et son groupe. L'auto-contrôle essaie ces trois refus sur un processus lié comme un plugin (`confine.unconfined_actions`).

Aucune garantie de réseau de cette ADR ne repose sur seccomp (seule l'étanchéité entre plugins s'y appuie : signaux avant l'ABI 6, changements de mode) : l'absence de réseau est tenue par `network_mode: none` et vérifiée par l'auto-contrôle. Le filtre seccomp du worker de décodage (M1a, `docs/decisions/M1a.md`) peut être posé sur chaque sous-processus en défense en profondeur ; aucun test d'isolation n'en dépend.

**Risque résiduel accepté** : les plugins d'un même compagnon partagent l'uid 1001 (un uid par plugin demanderait `CAP_SETUID`, refusé). Un plugin compromis peut gêner un autre plugin (signal, épuisement du `tmpfs`) ; la restriction `ptrace` de Landlock entre domaines frères empêche la lecture de sa mémoire (`PR_SET_DUMPABLE = 0` protège le compagnon, non un plugin : `exec` remet l'attribut). Rien de secret ne transite par le compagnon, ce qui borne l'enjeu. Écrit dans `docs/admin/plugins-isoles.md`.

### Le cœur n'exécute jamais ce que le compagnon renvoie
Tout ce qui vient du compagnon est une **donnée non fiable** :
- une réponse est un document JSON validé par les modèles stricts du SDK, puis rejetée si elle ne passe pas ; jamais `pickle`, jamais d'`object_hook`, jamais de nom de classe, de module ou de fonction pris dans un message ; jamais `eval`, `exec`, ni import ;
- un plan de destination (`list[FsOp]`, Q52) est validé par l'hôte avant exécution (racine, chemins réservés, recouvrements, ADR-0022) ; une `ApiCall` passe par le Fetcher et ses gardes ;
- une URL demandée par un plugin passe par la garde SSRF, l'allowlist du manifeste et la rédaction des journaux ; aucune chaîne venue d'un plugin n'est passée à un shell, à une requête SQL non paramétrée ni à un gabarit sans échappement ;
- en mode isolé, le cœur **n'importe jamais** de code du volume `plugins` ; il y installe des roues (l'installation d'une roue n'exécute aucun code) et en lit les métadonnées avec la garde de M1b.

### D13 — Mode par défaut d'un plugin tiers (délégué)
Un plugin tiers s'installe **désactivé** ; son mode par défaut est **isolé** ; l'activer est une **action réservée** (session fraîche ou CLI locale, `test_isolated_plugin_enable_is_reserved_action`). Le mode en processus garde `SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS=1` et la confirmation tapée de M1b. **Un plugin ne choisit jamais son mode** : ni le manifeste, ni l'index ne peuvent demander le mode en processus. L'attestation d'EF-EXP-065 s'applique telle quelle à un plugin isolé ; ses événements restent dans `plugin.<clé>.*`.

### D12 — `rpc_protocol` 1 (délégué)
- **Cadrage** : JSON-RPC 2.0 en trames préfixées par leur longueur (4 octets, gros-boutiste), **jamais par ligne**, sur le socket cœur–compagnon comme sur les tubes compagnon–plugin. Longueur lue avant toute allocation ; une trame de plus de **8 Mio** est refusée et la connexion du plugin fermée. `bytes` en base64, comptés dans la limite.
- **Décodage strict** : types JSON seulement ; `NaN`, `Infinity`, clés en double et profondeur supérieure à 64 refusés ; aucun objet vivant ne traverse la frontière (`test_rpc_never_carries_a_live_object`).
- **Poignée de main avant le manifeste** : le premier message est `hello {rpc_protocol: [1]}` ; hors plage, refus typé et arrêt du sous-processus **sans lire le manifeste** (`test_rpc_protocol_out_of_range_refused_before_manifest`). `api` est négociée ensuite, séparément (`test_plugin_subprocess_jsonrpc_rpc_protocol_distinct_from_api`).
- **Méthodes** : de l'hôte vers le plugin, celles des contrats `Source`, `ExportTarget`, `MetadataProvider` et `Tracker` ; du plugin vers l'hôte, `fetcher.get|post|download` et `events.publish` seulement, **pendant un appel de l'hôte** et rattachées à lui (`test_rpc_plugin_cannot_call_host_outside_a_host_call`). Une réponse à un identifiant inconnu est une trame invalide.
- **Bornes** : délai par défaut 30 s par appel (300 s pour un téléchargement), au plus 4 appels en vol par plugin, mémoire 512 Mio, CPU : plafond **cumulé** du processus (`RLIMIT_CPU`, 600 s par défaut), le délai par appel étant en temps mural (amendé par M6 L04 : un plafond par appel demande de suivre le processus, ce que le compagnon de L05 fera) (`test_rpc_message_size_and_deadline_bounded`). Le corps d'un téléchargement passe par trames successives bornées, avec un total plafonné ; jamais un message non borné.
- **Pannes** : plantage, sortie, délai dépassé ou trame invalide → plugin `error` avec la raison, l'hôte continue (`test_isolated_plugin_crash_host_survives_plugin_marked_error`).
- **Secrets** : aucun secret dans un message, l'environnement ou les arguments (`test_no_secret_passed_to_plugin`, `test_plugin_subprocess_environment_carries_no_secret`) ; le Fetcher injecte les secrets **côté hôte** et retire les en-têtes d'authentification et les cookies de session des réponses rendues au plugin (`test_rpc_fetcher_goes_through_ssrf_guard`).
- **Distinct du socket CLI** : autre socket, hors de `/data`, aucun code partagé avec `infrastructure/local_socket/` hormis l'encodage JSON.

### D14 — Format de l'index et de sa signature (délégué)
- **Fichiers** : `index.json` et sa signature détachée `index.json.minisig` ; pendant une rotation, une seconde signature `index.json.next.minisig`.
- **Signature** : format minisign préhaché (`ED` : Ed25519 sur le BLAKE2b-512 du fichier), vérifié avec `pynacl` ; l'ancien format non préhaché (`Ed`) est refusé ; l'identifiant de clé de la signature doit être celui de la clé épinglée ; la signature globale couvre le **commentaire de confiance**, qui vaut exactement `shelfhold-index v1 version=<n> expires=<date>` et doit égaler `version` et `expires` du document (`test_index_trusted_comment_bound_to_version`).
- **Document v1** : `format: 1`, `version` (entier), `expires` (date ISO 8601 UTC), `plugins[]` avec `{key, version, api, rpc_protocol, wheel_url, sha256, min_host, deprecated, replaced_by}`, `revoked` (`plugin_keys[]`, `wheel_sha256[]`), `next_pubkey` (seulement pendant une rotation).
- **Anti-retour arrière** : `version` strictement supérieure à `plugin_repo.last_version` → accepté ; égale avec un contenu identique → sans effet ; égale avec un contenu différent → refusé (deux index signés pour un même numéro) ; inférieure → refusé (`test_signed_index_monotonic_version_rejects_rollback`).
- **Expiration** : index expiré refusé (`test_index_expired_rejected`) ; un index dont `expires` dépasse de plus de **90 jours** la date de vérification est refusé. Un index expiré n'autorise aucune installation ni mise à jour ; les plugins installés continuent de tourner, marqués « index expiré ».
- **Révocation** : par clé de plugin et par empreinte de roue ; un plugin révoqué ne s'installe pas, et une installation existante est désactivée et marquée au rafraîchissement suivant. Le rafraîchissement ne touche ni le registre vivant du serveur ni le compagnon isolé : un plugin révoqué qui tourne déjà continue de servir jusqu'au prochain redémarrage. La réponse du rafraîchissement le dit (`restart_required`, les clés désactivées, en HTTP et en CLI) : l'arrêt à chaud est écarté, il passerait par le compagnon. Une révocation vue une fois **reste locale** même si un index plus récent l'omet (`test_revocation_and_double_signature_rotation`).
- **Rotation par double signature** : un index signé par l'ancienne **et** la nouvelle clé, dont `next_pubkey` désigne la nouvelle, fait passer l'épinglage à la nouvelle clé ; une nouvelle clé seule est refusée. Une clé ancienne **compromise** permet une rotation hostile : il n'y a pas de recours dans la bande ; l'admin retire le dépôt et le rajoute avec la nouvelle empreinte obtenue hors bande (écrit dans la doc d'hébergement).
- **Ajout d'un dépôt** : par URL **et** empreinte de la clé publique, action réservée (`test_index_add_is_reserved_and_needs_key_fingerprint`) ; table `plugin_repo(url, pubkey, key_id, last_version, expires, added_at)` ; **aucun dépôt préconfiguré** (`test_fresh_install_zero_repo_preconfigured`) ; rafraîchissement par le Fetcher et sa garde SSRF (`test_index_refresh_goes_through_fetcher`). Retirer puis rajouter un dépôt remet `last_version` à zéro : c'est une action réservée qui redemande l'empreinte.
- **Roue** : téléchargée par le Fetcher depuis `wheel_url`, **sha256 vérifié avant la garde de M1b** (`test_wheel_sha256_verified`), puis installée en `--only-binary :all: --require-hashes --no-deps --no-index` (`test_plugin_install_only_binary_require_hashes_no_deps`) dans le volume `plugins`. Aucune dépendance n'est résolue : un plugin qui en a besoin les embarque dans sa roue.

### D-P3 — Index officiel et clé de signature (recommandation, en attente du porteur)
Option 1 : M6 livre le **client** (vérification, épinglage, révocation, rotation) et la doc d'hébergement d'un index tiers, **sans index officiel**. Option 2 : un index officiel, clé générée **hors ligne** par le porteur, clé secrète hors de GitHub et de toute CI, clé publique dans `docs/plugins/index-officiel.md`, seconde clé prête pour la rotation. *Recommandation* : option 1 (aucun plugin tiers à lister ; un index vide engage une clé pour rien). Dans tous les cas : **aucune clé secrète littérale** dans le dépôt, les fixtures ni la CI ; les tests génèrent leurs clés dans le test (`pynacl`) et l'e2e signe l'index d'exemple avec une clé éphémère créée dans le job.

### Épinglage `(clé du plugin, index, clé du signataire)` (L08, EF-SRC-017 passe 8)
À l'installation, `plugin_install.signer_pubkey` et `index_url` épinglent le triplet (`test_plugin_key_pinned_to_signer`). Un autre index, ou un autre signataire, qui publie la même clé de plugin ne met jamais à jour l'installation : une **installation distincte** est proposée (`test_signer_change_proposes_distinct_install`) ; état, réglages et secrets ne sont transférés qu'après confirmation, action réservée (`test_state_and_secrets_moved_only_after_confirmation`). Une signature ne vaut pas confiance : la fiche de confiance reste « Inconnu » tant que rien n'est attesté (`test_signed_index_alone_never_makes_a_module_community`).

### D15 — Cycle de vie (délégué)
- **Mise à jour** manuelle, jamais automatique ; un plugin épinglé n'est jamais mis à jour par un rafraîchissement (`test_pinned_plugin_never_updated_by_refresh`).
- **Retour arrière** vers la seule version précédente gardée, action réservée (`test_rollback_is_reserved`) ; refusé, avec la raison, si l'état persisté (`state_written_by_version`) est plus récent que ce que la version visée sait lire (`test_rollback_refused_when_persisted_state_newer`), ou si la version visée est révoquée. Installer depuis l'index une version inférieure à celle installée **est** un retour arrière et suit les mêmes règles.
- **Migrations d'état** : l'hôte appelle `migrate_state(from_version)` avant le premier appel d'une nouvelle version ; le kit échoue quand `payload_schema` change sans elle (`test_plugin_state_migration_required_when_payload_schema_changes`).
- **Désinstallation** : liaisons marquées orphelines, contenu, `export_item` et positions intacts, sans limite de durée ; une réinstallation de la même clé chez le même signataire les rattache (`test_plugin_update_pin_rollback_uninstall_orphans`, `test_uninstalled_plugin_binding_orphaned_without_content_loss`).
- **Attestation** caduque à chaque changement de version (EF-EXP-065, `test_third_party_publisher_trust_requires_admin_attestation`).
- **Montée de l'hôte** : un plugin hors `min_host` ou hors `api` est marqué et `plugin.incompatible` est émis une fois (`test_plugin_incompatible_event_after_host_upgrade`).
- **Champs d'index** : `deprecated`, `replaced_by`, `min_host` lus (`test_index_fields_deprecated_replaced_by_min_host`) ; `replaced_by` n'installe jamais le remplaçant (autre clé = nouvelle installation, avec ses propres contrôles) ; une migration `replaced_by` passe par `tombstone_check` avec l'alias inverse (`test_identity_migration_consults_tombstones`, cas `replaced_by`).

### D16 — ABI (délégué)
Le compagnon tourne dans la même image que le cœur, donc avec le même interpréteur. Au démarrage (et à l'installation), les étiquettes de chaque roue sont comparées à l'interpréteur ; une roue `py3-none-any` reste compatible. Une roue incompatible est marquée « ABI incompatible », jamais exécutée, et une réinstallation guidée est proposée (ENF-OPS-018, `test_abi_incompatible_plugin_flagged_after_python_upgrade`). Politique « Python N+1 un an avant la fin de vie de N » écrite dans `docs/admin/python-et-plugins.md`.

### D10, D11, D24 — SDK, nom d'import, `api` 2, versions (délégués)
- **Nom d'import (D10)** : la distribution `shelfhold-sdk` fournit **`shelfhold_sdk`**. Elle ne peut pas fournir `shelfhold.sdk` sans paquet d'espace de noms, et la garde de M1b refuserait un plugin qui apporte un paquet `shelfhold` en conflit avec l'hôte. `shelfhold.sdk` réexporte `shelfhold_sdk` pendant la série 1.x de l'application (une dépréciation écrite) et disparaît avec `api` 1 ; plugins d'exemple, gabarit et kit (`shelfhold_sdk.testing`) importent `shelfhold_sdk`.
- **`api` 2 (D11, Q20 tranchée)** : première version publiée `2.0.0` = `api` 2, sans les membres Kavita dépréciés ; l'hôte charge `api` 1 par le shim pendant **12 mois** après la publication, écrit dans la politique de versions (ENF-OPS-008).
- **Versions (D24)** : l'application sort en **`v1.3.0`** (mineure : aucune rupture de l'API HTTP, compagnon optionnel), le SDK en **`2.0.0`**, composant release-please séparé.
- Le manifeste déclare `api` et `rpc_protocol` séparément ; MIT inchangé (ADR-0014).

### D-P2 — Publication de `shelfhold-sdk` sur PyPI (recommandation, en attente du porteur)
Recommandation : publication de confiance OIDC par `publish-sdk.yml` (étiquettes `sdk-v*`, environnement `pypi` protégé avec le porteur pour relecteur, `id-token: write` sur ce seul job, action épinglée par SHA, attestations PEP 740, **aucun secret référencé**), d'abord sur TestPyPI, puis sur PyPI à la clôture. Les comptes, les éditeurs de confiance en attente et les environnements GitHub sont des actions du porteur ; un numéro publié ne se republie jamais. *Repli* : job `sdk-dist` à chaque PR (roue, sdist, contrôle des métadonnées, installation **sans l'hôte** et passage du kit) ; sans action du porteur, `[[partial]]` ENF-SEC-007 « première publication sur PyPI ».

### D-P4 — Relecture de l'exception de plugin, Q17 (recommandation, en attente du porteur)
Le SDK est sous MIT : sa publication n'a pas besoin de l'exception. Un plugin **isolé** ne parle au cœur que par `rpc_protocol` et ne se lie à aucun code AGPL ; l'exception ne sert qu'au mode **en processus**, qui reste derrière la garde de M1b. Deux options :
- **relecture par un juriste** avant `v1.3.0`, consignée dans `docs/legal/` ;
- **levée écrite** de l'exigence de relecture, comme Q27a (risque accepté, consigné dans 06 Q17 et `docs/legal/`).

*Recommandation du plan* : **découpler**. M6 avance sans attendre ; la relecture (ou la levée) ne conditionne que (a) le texte définitif de l'exception et (b) l'annonce des plugins tiers en processus. *Repli* sans réponse à la clôture : l'en-tête « provisoire » reste, et `docs/plugins/` recommande le mode isolé et une licence compatible AGPL pour le mode en processus.

### D-P5 — Critère de la sync Kobo, Q21 (critère écrit ; question ouverte)
Critère fixé à la clôture de M5 : EF-INT-012 est tranchée quand **Kavita `0.9.2` est publiée** avec la synchronisation Kobo **filtrée par bibliothèque et par plafond d'âge** (page des versions publiées, pas la branche de développement). Question au porteur, **ouverte** : un filtre par bibliothèque suffit-il là où Shelfhold raisonne par étagère réservée à un profil ? Contrôle à la V1, puis à l'entrée de V10 : si `0.9.2` est publiée avec les deux filtres **et** que le porteur répond « la bibliothèque suffit », L17 n'est pas lancé et EF-INT-012 passe en « Jamais » par amendement (L25) ; sinon L17a et L17b partent (coupables). Coupe au-delà de 240 h : `[[deferred]]` EF-INT-012. Résultat consigné dans `docs/decisions/M6.md`.

### D-P6 — Premier tracker first-party (recommandation, en attente du porteur)
Recommandation : M6 livre le contrat `Tracker` (SDK), l'hôte par profil et un faux tracker dans le kit, ce qui prouve EF-INT-010 ; un tracker réel (L14, coupable) n'est lancé que si le porteur nomme le service et dit en avoir lu les conditions d'utilisation de l'API. *Repli* : sans réponse avant la V9, L14 est coupé.

## Tests et décisions
Chaque test des cartes L04 à L09b et de 03 §M6 est rattaché à une décision.

| Décision | Tests |
|---|---|
| D12 (`rpc_protocol` 1, L04) | `test_plugin_subprocess_jsonrpc_rpc_protocol_distinct_from_api`, `test_isolated_plugin_crash_host_survives_plugin_marked_error`, `test_rpc_message_size_and_deadline_bounded`, `test_rpc_fetcher_goes_through_ssrf_guard`, `test_rpc_never_carries_a_live_object`, `test_rpc_plugin_cannot_call_host_outside_a_host_call`, `test_rpc_protocol_out_of_range_refused_before_manifest` |
| D-P1, propriétés et auto-contrôle (L05) | `test_plugin_subprocess_never_sees_data_socket` (kit, passe 13), `test_plugin_subprocess_uid_distinct_from_core`, `test_plugin_subprocess_has_no_network`, `test_plugin_runner_refuses_to_start_when_it_sees_data`, `test_plugin_subprocess_environment_carries_no_secret`, `test_core_never_listens_on_the_runner_volume`, `test_no_secret_passed_to_plugin` (mode isolé) |
| D13, garde dans les deux modes (L06) | `test_third_party_plugins_disabled_by_default`, `test_unsandboxed_flag_requires_typed_confirmation`, `test_plugin_site_packages_after_host_conflicting_names_refused`, `test_plugin_install_only_binary_require_hashes_no_deps`, `test_no_secret_passed_to_plugin` (mode en processus), `test_third_party_publisher_trust_requires_admin_attestation` (passe 10), `test_isolated_plugin_enable_is_reserved_action`, `test_isolated_plugin_events_namespaced_over_rpc` |
| D14 (index, L07) | `test_signed_index_monotonic_version_rejects_rollback`, `test_index_expired_rejected`, `test_wheel_sha256_verified`, `test_revocation_and_double_signature_rotation`, `test_fresh_install_zero_repo_preconfigured`, `test_index_trusted_comment_bound_to_version`, `test_index_refresh_goes_through_fetcher`, `test_index_add_is_reserved_and_needs_key_fingerprint` |
| Épinglage (L08, passe 8) | `test_plugin_key_pinned_to_signer`, `test_signer_change_proposes_distinct_install`, `test_state_and_secrets_moved_only_after_confirmation`, `test_signed_index_alone_never_makes_a_module_community` |
| D15 (L09a) | `test_plugin_update_pin_rollback_uninstall_orphans`, `test_uninstalled_plugin_binding_orphaned_without_content_loss`, `test_pinned_plugin_never_updated_by_refresh`, `test_rollback_is_reserved` |
| D15, D16 (L09b, passes 4 et 11) | `test_plugin_state_migration_required_when_payload_schema_changes`, `test_rollback_refused_when_persisted_state_newer`, `test_plugin_incompatible_event_after_host_upgrade`, `test_abi_incompatible_plugin_flagged_after_python_upgrade`, `test_index_fields_deprecated_replaced_by_min_host`, `test_identity_migration_consults_tombstones` (cas `replaced_by`) |
| D-P2 | workflow `publish-sdk` sans jeton longue durée (OIDC), job `sdk-dist` |
| Hors de cette ADR (03 §M6) | D17 : `test_cache_retention_quotas` ; D18, D-P6 : `test_trackers_per_user_forbidden_for_child_by_default` ; D-P5, D19, D20 : `test_kobo_initialization_and_dummy_auth_device`, `test_kobo_library_sync_entitlements_tags_continuation_token`, `test_kobo_state_get_put`, `test_kobo_per_device_token_in_path`, `test_kobo_kepubify_cached`, `test_kobo_store_proxy_off_by_default_opt_in_per_device`, `test_kobo_destination_kepubify` (le nom garde « kepubify » ; D19 : transformation Python de M5, aucun binaire) |

## Conséquences
- Positives : un plugin tiers compromis est contenu par le moteur de conteneurs (uid distinct, ni `/data` ni socket, aucun réseau, lecture seule), et le cœur ne fait confiance à rien de ce qu'il renvoie ; le réseau reste contrôlé en un point (Fetcher du cœur) ; l'index donne révocation, expiration bornée, anti-retour arrière et rotation ; le SDK publie un contrat sous un nom d'import qui ne heurte pas l'hôte.
- Négatives : le porteur modifie son compose ; un service de plus ; un protocole RPC et un format d'index à maintenir ; le shim `api` 1 pendant 12 mois ; aucune dépendance résolue pour un plugin (il les embarque).

## Risque principal
Une garantie d'isolation contournée (volume monté par erreur dans le compagnon, réseau laissé ouvert par un compose modifié). Mitigation : auto-contrôle qui refuse de démarrer, contrôle `SO_PEERCRED` côté cœur, compose d'e2e en CI, kit en mode isolé à chaque passage, et cœur qui traite le compagnon comme hostile.

## Réversibilité
Haute pour le transport et l'index (versionnés, révocables). Moyenne pour l'isolation (changer de modèle d'exécution casserait les plugins isolés existants). Faible pour une clé publique annoncée (D-P3, option 2) et pour un numéro publié sur PyPI (D-P2).

## Exigences liées
EF-SRC-016, EF-SRC-017, EF-SRC-019, EF-SRC-020, EF-SRC-030, EF-BIB-054, EF-EXP-001, EF-EXP-021, EF-EXP-065, EF-INT-010, EF-INT-012, EF-USR-024, ENF-SEC-007, ENF-SEC-009, ENF-SEC-013, ENF-OPS-008, ENF-OPS-018, ENF-QUAL-003, ENF-LEG-001, ENF-LEG-010.

## Sources
- ADR-0007, ADR-0014, ADR-0015, ADR-0016, ADR-0017, ADR-0022, ADR-0024, ADR-0036
- `docs/spec/06-points-discutables.md` (Q17, Q20, Q21, Q52)
- https://jedisct1.github.io/minisign/ (format de signature, commentaire de confiance, préhachage)
- https://www.jsonrpc.org/specification
- https://peps.python.org/pep-0740/ (attestations de publication)
- https://docs.docker.com/reference/compose-file/services/ (`network_mode`, `read_only`, `cap_drop`, `security_opt`, `pids_limit`)
- https://github.com/hashicorp/go-plugin (référence d'architecture : poignée de main, processus par plugin)

## Relecture adverse (sécurité, 2026-11-20)
Relecture du premier jet de cette PR. Gravité : **bloquante** (l'isolation ou la signature ne tiendrait pas), **majeure** (décision fausse ou non traçable), **mineure**.

| # | Objection | Gravité | Correction |
|---|---|---|---|
| 1 | Le compagnon était « lancé par le cœur » dans son conteneur, sous-processus restreints par espaces de noms : un processus `uid 1000` sans capacité ne peut ni changer d'uid, ni couper le réseau, ni retirer `/data`. L'option « conteneur séparé » était écartée alors que c'est la recommandation du plan. | bloquante | Option A (service compose `plugin-runner`, 1001, `network_mode: none`, lecture seule, `cap_drop: [ALL]`, sans `/data`) ; B et C refusées avec leur raison ; repli sans compagnon. |
| 2 | Rien n'interdisait au cœur d'exécuter ce que le compagnon renvoie ; le compagnon était traité comme sûr. | bloquante | Section « Le cœur n'exécute jamais ce que le compagnon renvoie » : validation stricte, ni `pickle` ni import, plans validés par l'hôte, URL par les gardes du Fetcher. |
| 3 | `rpc_protocol` 1 « JSON-RPC sur stdio » sans cadrage, sans taille maximale, sans délai, sans limite d'appels en vol, sans poignée de main avant le manifeste, sans règle pour les appels du plugin vers l'hôte. | bloquante | D12 complète : trames préfixées, 8 Mio, décodage strict, `hello` avant le manifeste, appels du plugin seulement pendant un appel de l'hôte, délais et plafonds. |
| 4 | Index : pas de commentaire de confiance lié à la version, ancien format de signature accepté, pas de durée maximale, même numéro avec deux contenus accepté, révocation par empreinte de roue absente et non persistante, rotation sans désignation de la nouvelle clé, `wheel_url` absent, `--no-deps --no-index` absents. | bloquante | D14 réécrite selon le plan, plus : refus de l'équivoque à numéro égal, révocation persistante, `next_pubkey`, compromission de l'ancienne clé écrite. |
| 5 | Statut « acceptée » alors que D-P1 n'est pas confirmée par le porteur. | majeure | Statut « proposée ». |
| 6 | « D-P4 tranchée : relecture, pas de levée », contraire au plan, qui recommande de découpler et laisse le choix au porteur. Repris dans ADR-0014, ADR-0036, 06 Q17 et le registre. | majeure | Deux options et la recommandation, « en attente du porteur », partout. |
| 7 | Numéros de décision faux : D-P3 désignait les propriétés d'isolation (c'est l'index officiel), D13 l'épinglage (c'est le mode par défaut), D25 « licence » et D26 « stratégie de tests » (ce sont l'allowlist et les formats de position). D13 réelle et D-P3 réelle étaient absentes. | majeure | Numérotation du plan ; sections D13 et D-P3 ajoutées ; D25 et D26 renvoyées à leurs lots. |
| 8 | Nom d'import `shelfhold.sdk` : une distribution séparée ne peut pas le fournir sans espace de noms, et un paquet `shelfhold` apporté par un plugin serait refusé par la garde de M1b (conflit de noms). | majeure | D10 du plan : `shelfhold_sdk`, réexport temporaire par `shelfhold.sdk`. Corrigé dans ADR-0007, 0014, 0024, 02 et 04. |
| 9 | Absence de réseau garantie « par seccomp », qu'aucun lot de M6 ne pose sur les plugins (le filtre de M1a ne couvre que le worker de décodage). | majeure | Garantie tenue par `network_mode: none` et l'auto-contrôle ; seccomp seulement en défense en profondeur. |
| 10 | Auto-contrôle incomplet (`/store`, `/library`, capacités, `NoNewPrivs`, connexion sortante) et qui laissait le compagnon tourner ; « re-vérifiées par le cœur » sans moyen. | majeure | Le compagnon refuse de démarrer ; liste complète ; `SO_PEERCRED` lu par le cœur, qui n'écoute jamais dans le volume du compagnon. |
| 11 | Secrets : environnement du sous-processus non vidé, réponses du Fetcher pouvant rendre des en-têtes d'authentification. | majeure | Environnement en liste fermée ; secrets injectés côté hôte et retirés des réponses. |
| 12 | Épinglage : la signature pouvait passer pour de la confiance ; l'ajout d'un dépôt sans empreinte ni action réservée. | majeure | Fiche « Inconnu » tant que rien n'est attesté ; ajout par URL et empreinte, action réservée. |
| 13 | Cycle de vie : retour arrière déguisé (installer une version inférieure listée par l'index), version précédente révoquée, `replaced_by` qui installerait le remplaçant. | majeure | Règles écrites dans D15. |
| 14 | D-P2 présentée comme acquise (« publié en M6 ») alors que la publication est une action du porteur, en partie irréversible. | majeure | Recommandation en attente, repli `sdk-dist` et `[[partial]]`. |
| 15 | D-P5 : critère déformé (date du 2026-11-15, « bibliothèques restreintes », « à l'entrée de M6 ») au lieu de Kavita `0.9.2` avec deux filtres, contrôle à V1 et V10, question au porteur ouverte. Repris dans 01, 03 et 06. | majeure | Critère du plan, partout. |
| 16 | Tests des cartes L04 à L09b absents de l'ADR (15 noms) ; tests de 03 §M6 non rattachés. | majeure | Table « Tests et décisions ». |
| 17 | Les plugins d'un même compagnon partagent l'uid 1001. | mineure | Risque résiduel accepté et écrit ; `PR_SET_DUMPABLE = 0`. |
| 18 | 01 EF-EXP-021 : « kepubify à l'export », contraire à D19 (transformation Python, aucun binaire). | mineure | 01 corrigé. |
| 19 | « L'index officiel ne liste aucun module… » présupposait un index officiel (D-P3 ouverte). | mineure | « Un index, quel qu'il soit » ; D-P3 en attente. |
