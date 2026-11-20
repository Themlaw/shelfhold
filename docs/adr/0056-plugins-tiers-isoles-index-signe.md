---
status: acceptée
date: 2026-11-20
decision-makers: mainteneur
---
# ADR-0056 — Plugins tiers isolés (compagnon `plugin-runner`) et index signé

> Décisions du porteur : **D-P1** (compagnon `plugin-runner`), **D-P3** (propriétés garanties et auto-contrôle), **D-P4** (relecture de l'exception de plugin, Q17), **D-P5** (critère écrit de la sync Kobo, Q21). Décisions déléguées : **D10**–**D16**, **D24**, avec **D25** (licence) et **D26** (stratégie de tests). Amende ADR-0007, ADR-0014, ADR-0016, ADR-0024 et ADR-0036 ; corrige 01, 02, 03, 04 et 06.

## Contexte et problème
M6 ouvre Shelfhold aux plugins tiers (EF-SRC-016, EF-SRC-017, EF-SRC-019, EF-SRC-020). ADR-0007 fixe le contrat unique et reporte l'isolation « en M6 » sans la décrire ; ADR-0024 fige le contrat de données `api` v1 et la compatibilité N-1. Avant tout code, il faut décider : le compagnon d'exécution des plugins isolés, le protocole RPC et son versionnage, le format de l'index signé et son épinglage, le cycle de vie des installations, l'ABI, et les versions publiées (application et SDK).

Contraintes déjà actées : aucun sous-processus de plugin ne voit `/data` ni le socket de la CLI locale et aucun ne partage l'uid du cœur (GAP-13-07, EF-USR-024, `test_plugin_subprocess_never_sees_data_socket`) ; installation `--only-binary :all: --require-hashes`, sans résolution de dépendances, dans un `site-packages` séparé (ENF-SEC-009) ; plugins tiers désactivés par défaut (EF-SRC-019) ; aucun secret transmis à un plugin (EF-EXP-001, ENF-SEC-013) ; l'index officiel ne liste aucun module de retrait de mesure technique de protection (ADR-0015, ENF-LEG-001).

## Facteurs de décision
- Défense en profondeur : un plugin compromis ne doit ni lire `/data`, ni parler au socket CLI, ni sortir sur le réseau, ni écrire sur le disque, ni hériter de l'uid du cœur.
- Fail-closed : l'auto-contrôle au démarrage du runner bloque le lancement d'un plugin si une garantie n'est pas tenue.
- Un seul sens pour les connexions : le plugin n'initie jamais rien.
- Le transport (`rpc_protocol`) est versionné séparément du contrat de données (`api`) et du socket CLI.
- L'index est signé, monotone, expirable, révocable, avec sha256 par wheel ; zéro dépôt préconfiguré.
- SemVer : l'application sort en `v1.3.0` à M6 (D24), le SDK en `2.0.0` avec `api` 2 et un shim `api` 1 de 12 mois (Q20 tranchée).

## Options considérées
### A. Compagnon `plugin-runner` dédié, uid et namespaces restreints (choisi)
- Avantages : le cœur ne lance jamais directement un plugin ; les garanties (uid, `/data`, réseau, lecture seule) sont tenues par construction et vérifiées par auto-contrôle ; un crash ou une compromission du plugin ne touche pas le cœur.
- Inconvénients : un processus de plus à superviser ; un protocole RPC à versionner.
### B. Sous-processus lancé directement par le cœur
- Avantages : plus simple.
- Inconvénients : l'uid du cœur est exposé (GAP-13-07), `/data` et le socket CLI restent visibles sauf à monter un namespace par plugin ; la supervision est dispersée dans le cœur.
### C. WASM (Extism)
- Avantages : vraie sandbox.
- Inconvénients : PDK Python quasi dormant (déjà écarté en ADR-0007) ; pas de parseur HTML embarqué.
### D. Conteneur séparé
- Avantages : isolation forte.
- Inconvénients : lourd pour un auto-hébergé (pas de Docker dans l'image), pas de supervision fine par plugin.

Pour l'index : **A. index JSON signé minisign** (choisi) ; B. PyPI seul (écarté : pas de révocation propre, pas de rotation de clé, dépôt unique, pas d'expiration) ; C. fédération d'index (plus tard, hors M6).

## Décision

### D-P1 — Compagnon `plugin-runner`
Un compagnon **`plugin-runner`** (point d'entrée livré dans l'image, même version de Python que le cœur) est lancé par le cœur à l'activation du premier plugin tiers isolé. Le cœur ne parle **jamais** directement à un plugin :

```
cœur ──socket Unix dédié (hors /data)──▶ plugin-runner ──stdio (pipes)──▶ plugin
```

Le runner supervise les processus des plugins (un par plugin), applique les limites de ressources (RLIMIT_CPU, RLIMIT_AS, RLIMIT_NPROC, RLIMIT_FSIZE, `pids_limit`), et ne relaie que des messages JSON sérialisables.

### D-P3 — Propriétés garanties, auto-contrôle au démarrage, sens des connexions
Propriétés garanties pour le runner et chaque plugin, **vérifiées par auto-contrôle au démarrage du runner** (fail-closed : si un contrôle échoue, aucun plugin ne démarre et l'hôte reçoit un événement typé) et re-vérifiées par le cœur :

- **uid distinct** : runner et plugins sous un uid dédié, jamais celui du cœur (GAP-13-07) ;
- **ni `/data` ni socket CLI** : namespace de montage sans `/data` et sans `/data/shelfhold.sock` (`test_plugin_subprocess_never_sees_data_socket`) ;
- **aucun réseau** : pas d'interface réseau (pas de loopback utile), `socket(AF_INET/AF_INET6)` refusé par seccomp ; tout HTTP passe par le Fetcher du cœur, appelé par RPC ;
- **lecture seule** : racine en read-only, seul un tmpfs scratch par plugin, taille bornée ;
- **aucun secret** : aucun secret du trousseau n'est transmis au runner ni au plugin.

**Sens des connexions** : strictement cœur → runner → plugin. Le plugin n'initie aucune connexion (stdio hérité uniquement) ; le runner n'accepte que la connexion du cœur, contrôlée par `SO_PEERCRED` (uid du cœur attendu). Aucune connexion entrante n'est possible depuis le plugin.

### D12 — `rpc_protocol` 1
Transport **JSON-RPC 2.0 sur stdio**, versionné séparément : le handshake déclare `rpc_protocol: 1`. `rpc_protocol` est distinct de `api` (contrat de données du SDK, ADR-0024) et du protocole du socket CLI local (`/data/shelfhold.sock`, `SO_PEERCRED`). Le cœur refuse tout plugin dont le `rpc_protocol` déclaré n'est pas supporté (`test_plugin_subprocess_jsonrpc_rpc_protocol_distinct_from_api`). Seuls des types sérialisables en JSON traversent la frontière : aucun objet vivant (`Fetcher`, mandataire `f` d'opérations fichier) ne la traverse — les destinations rendent un **plan `list[FsOp]`** que l'hôte valide et exécute (Q52, ADR-0024 passe 8).

### D14 — Format de l'index et de sa signature
L'index d'un dépôt est un document JSON `index.json` à l'URL déclarée (table `plugin_repo` : `url`, `pubkey`, `last_version`, `expires`), accompagné d'une signature détachée **minisign** `index.json.minisig` :

- `index_version: 1` ; `version` entier **monotone** (anti-rollback : un index de version inférieure est refusé, `test_signed_index_monotonic_version_rejects_rollback`) ; `expires` (date ISO ; index expiré refusé, `test_index_expired_rejected`) ;
- `plugins[]` : `key`, `name`, `versions[]` avec `version`, `api`, `rpc_protocol`, `wheels[]` (`filename`, `sha256`, `requires_python`), `deprecated`, `replaced_by`, `min_host` ;
- `revocations[]` : `key`, `version`, `reason`, `at` (signées) ;
- rotation de la clé de signature par **double signature** (ancienne + nouvelle) pendant une version.

À l'installation, le sha256 de chaque wheel est vérifié (`test_wheel_sha256_verified`) ; l'installation se fait `--only-binary :all: --require-hashes`, sans résolution de dépendances, dans un `site-packages` séparé (ENF-SEC-009). **Zéro dépôt préconfiguré** à l'installation neuve (`test_fresh_install_zero_repo_preconfigured`) ; l'index officiel ne liste aucun module de retrait de mesure technique de protection (ADR-0015).

### D13 — Épinglage `(clé, index, clé du signataire)`
À l'installation, l'hôte épingle le triplet **(clé du plugin, URL de l'index, clé publique du signataire)** dans `plugin_install` (`signer_pubkey`, `index_url`). Un changement de signataire est une **nouvelle installation**, jamais une mise à jour ; l'état, les réglages et les secrets ne sont transférés qu'après confirmation explicite de l'admin (EF-SRC-017, `test_plugin_key_pinned_to_signer`).

### D15 — Cycle de vie
Mise à jour (depuis l'index signé), **épinglage** sur une version (`pin`), **retour arrière** (refusé si l'état persisté est plus récent que la version visée, `test_rollback_refused_when_persisted_state_newer`), **désinstallation**, **liaisons orphelines** (une étagère ou une destination liée à un plugin désinstallé passe en état orphelin avec action requise, jamais de suppression silencieuse) — EF-SRC-020, `test_plugin_update_pin_rollback_uninstall_orphans`. Après une montée de version de l'hôte, un plugin incompatible émet l'événement `plugin.incompatible` (`test_plugin_incompatible_event_after_host_upgrade`).

### D16 — ABI
Le runner embarque le **même interpréteur Python** que l'image (même version mineure). Chaque wheel déclare `requires_python` et son tag ABI. Après une montée de Python de l'image, le runner re-vérifie l'ABI de chaque plugin installé : un plugin ABI-incompatible est **marqué et désactivé** (événement `plugin.incompatible`), jamais exécuté (ENF-OPS-018, `test_abi_incompatible_plugin_flagged_after_python_upgrade`).

### D10, D11, D24 — SDK 2.0.0, nom d'import, `api` 2 + shim `api` 1, versions
- **Distribution** : paquet PyPI **`shelfhold-sdk` 2.0.0**, publié en M6 par **publication de confiance OIDC** (aucun jeton longue durée, attestations PEP 740 ; ADR-0016, workflow `publish-sdk.yml`, tag `sdk-v2.0.0`), licence **MIT** (D25, ADR-0014) ;
- **Nom d'import (D10)** : **`shelfhold.sdk`**, identique au paquet interne du monorepo — aucun changement d'import pour le code first-party ;
- **`api` 2 (D11)** : le SDK 2.0.0 déclare `api = 2` ; l'hôte accepte `api` 2 et `api` 1 via un shim. **Q20 tranchée** : `api` 1 est supporté pendant **12 mois** après la sortie de 2.0.0, puis retiré au SDK 3.0. Les membres propres à Kavita dépréciés depuis la série 1.x sont retirés au SDK 2.0 (ADR-0024, amendement SDK 1.4) ;
- **Versions (D24)** : M6 sort l'application en **`v1.3.0`** (et non `v2.0.0`) ; le SDK a son propre SemVer (2.0.0), composant release-please séparé ;
- le **kit de conformité** `shelfhold.sdk.testing` est publié avec le SDK (D26, ADR-0017, ENF-QUAL-003) et s'exécute aussi bien dans le runner que dans l'adaptateur natif.

### D-P4 — Relecture de l'exception de plugin (Q17) : relecture, pas de levée
**Relecture.** La relecture juridique de l'exception de plugin (Q17) a lieu **avant la publication de M6** (SDK 2.0.0 + index signé) et est consignée par écrit ; l'exception reste **provisoire** jusque-là (ADR-0014, ADR-0036). La levée (retrait de l'exception) n'est pas retenue : les plugins tiers isolés tournent hors processus via le runner et ne dépendent pas de l'exception ; seuls les plugins natifs en processus (first-party) en dépendent.

### D-P5 — Critère écrit de la synchronisation Kobo (Q21)
Critère écrit, appliqué à l'entrée de M6 : le mainteneur relit les **notes de version publiées** de Kavita parues depuis le 2026-11-15. Si une version publiée livre la synchronisation Kobo **en respectant le contrôle de profil** (bibliothèques restreintes), EF-INT-012 n'est pas réalisée en M6 (passée en « Jamais » ou reportée) ; sinon, elle est réalisée (surface minimale, jeton par appareil, proxy magasin désactivé par défaut). Le résultat est consigné dans `docs/decisions/M6.md`.

## Conséquences
- Positives : un plugin tiers compromis est contenu (uid distinct, pas de `/data`, pas de réseau, lecture seule) ; le réseau reste contrôlé en un point (Fetcher du cœur) ; l'index signé donne révocation, expiration et anti-rollback ; le SDK 2.0.0 publie un contrat complet avec une fenêtre de migration de 12 mois.
- Négatives : un processus compagnon à superviser ; un protocole RPC et un index à maintenir ; le shim `api` 1 à garder 12 mois.

## Risque principal
Une garantie d'isolation contournée (ex. fuite du socket CLI ou de `/data` dans le namespace du runner, ou connexion réseau résiduelle). Mitigation : auto-contrôle au démarrage fail-closed, `SO_PEERCRED` des deux côtés, seccomp, tests dédiés (`test_plugin_subprocess_never_sees_data_socket`, `test_no_secret_passed_to_plugin`, `test_plugin_install_only_binary_require_hashes_no_deps`, `test_plugin_site_packages_after_host_conflicting_names_refused`), relecture adverse sécurité de cette ADR.

## Réversibilité
Haute pour le transport et l'index (versionnés, révocables). Moyenne pour l'isolation (changer de modèle d'exécution casserait les plugins isolés existants, d'où le shim `api` 1). Le passage à `api` 3 retirera le shim 1 (12 mois après 2.0.0).

## Exigences liées
EF-SRC-016, EF-SRC-017, EF-SRC-019, EF-SRC-020, EF-SRC-030, EF-EXP-001, EF-EXP-021, EF-EXP-065, EF-INT-012, EF-USR-024, ENF-SEC-009, ENF-SEC-013, ENF-OPS-018, ENF-QUAL-003, ENF-LEG-001, ENF-LEG-010.

## Sources
- ADR-0007, ADR-0014, ADR-0015, ADR-0016, ADR-0017, ADR-0024, ADR-0036
- `docs/spec/06-points-discutables.md` (Q17, Q20, Q21, Q52)
- https://github.com/jedisct1/minisign
- https://www.jsonrpc.org/specification.html
- https://peps.python.org/pep-0740/ (attestations de publication PyPI)
- https://github.com/hashicorp/go-plugin (référence d'architecture : handshake, SO_PEERCRED, kill par processus)
- `test_plugin_subprocess_jsonrpc_rpc_protocol_distinct_from_api`, `test_signed_index_monotonic_version_rejects_rollback`, `test_index_expired_rejected`, `test_wheel_sha256_verified`, `test_revocation_and_double_signature_rotation`, `test_fresh_install_zero_repo_preconfigured`, `test_plugin_key_pinned_to_signer`, `test_plugin_update_pin_rollback_uninstall_orphans`, `test_abi_incompatible_plugin_flagged_after_python_upgrade`, `test_plugin_subprocess_never_sees_data_socket`, `test_third_party_plugins_disabled_by_default`, `test_unsandboxed_flag_requires_typed_confirmation`, `test_plugin_install_only_binary_require_hashes_no_deps`, `test_plugin_site_packages_after_host_conflicting_names_refused`, `test_no_secret_passed_to_plugin`, `test_rollback_refused_when_persisted_state_newer`, `test_plugin_incompatible_event_after_host_upgrade`
