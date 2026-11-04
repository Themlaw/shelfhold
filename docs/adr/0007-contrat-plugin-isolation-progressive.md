---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 1)
decision-makers: mainteneur
---
# ADR-0007 — Contrat de plugin unique et isolation progressive

> **Amendée le 2026-11-02 (passe 1).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Shelfhold doit accueillir des sources, des fournisseurs de métadonnées, des trackers, des importeurs, des cibles d'export, des notifiers et des processors (D38), écrits par des tiers et parfois non fiables. Il faut un contrat stable, des tests de conformité et une voie d'isolation, sans bloquer la V1.

## Facteurs de décision
- Facilité d'écriture (un fichier Python).
- Contrôle centralisé du réseau (SSRF, allowlist, rate-limit).
- Isolation des plugins non fiables.
- Versionnement (`api` majeur) et testabilité (TESTS embarqués à la yt-dlp).

## Options considérées
### A. Modules en processus (entry points `importlib.metadata`)
- Avantages : surcoût nul, débogage facile (Beets, Paperless, lncrawl).
- Inconvénients : pas de sandbox ; installer un plugin revient à faire confiance à son code.
### B. Sous-processus JSON-RPC stdio
- Avantages : n'importe quel langage, isolation des crashs, privilèges réductibles (pas de réseau, sorties seulement via le Fetcher de l'hôte).
- Inconvénients : surcoût de processus ; protocole à versionner.
### C. WASM (Extism)
- Avantages : vraie sandbox, `allowed_hosts`, limites mémoire et temps.
- Inconvénients : PDK Python quasi dormant (26 étoiles, dernier push 2025-06) ; parseur HTML à embarquer dans chaque plugin ; débogage pénible.
### D. Déclaratif YAML (façon Cardigann)
- Avantages : sûr (des données, pas du code), couvre la longue traîne des thèmes Madara.
- Inconvénients : échoue sur le JS et les jetons signés.

## Décision
D8, D9, D10, D38 : **un seul contrat** (paquet `shelfhold-sdk`, MIT, publié en M6 — voir amendement, semver, `api` majeur entier) et plusieurs adaptateurs :
- V1 : (a) plugins Python natifs (entry points) et (c) méta-sources.
- V2 (M6) : (b) moteur YAML et (d) sous-processus JSON-RPC, **conçu dès la V1** avec le même contrat et les mêmes tests.
- Plus tard : WASM.

Règle absolue : **un plugin ne fait jamais d'I/O réseau ou disque** ; il reçoit le `Fetcher` (curl_cffi, rate-limit par domaine, cookies chiffrés, cache, garde SSRF post-DNS, allowlist par plugin) et rend des données ou des requêtes. Le manifest déclare `key`, `version`, `api`, `langs`, `content_kinds`, `url_patterns`, `capabilities`, `settings_schema`, `rate_limit`, `content_warning`, `tests`. Distribution V2 : index JSON signé minisign, ajout par URL + empreinte, permissions affichées, **aucun dépôt préconfiguré**.

## Conséquences
- Positives : un même kit de conformité pour tous les adaptateurs ; le réseau est contrôlé en un point ; les plugins se testent sur cassettes.
- Négatives : les plugins natifs V1 restent du code de confiance ; il faut un pont RPC pour le Fetcher.

## Risque principal
« La sandbox, on la fera plus tard » ne se fait jamais (avocat du diable de 04-backend) (R8). Mitigation : protocole RPC spécifié et kit de conformité exécuté dans les deux modes dès que l'adaptateur existe ; les plugins tiers sont marqués « non vérifiés » ; aucun index tiers n'est configuré en V1.

## Réversibilité
Moyenne : le contrat isole les plugins de l'hôte, mais une rupture du contrat `api` majeur casse tout l'écosystème.

## Amendement passe 1 (2026-11-02)
- **SDK interne en V1** (A7) : le contrat vit dans le monorepo (`shelfhold.sdk`) ; le paquet `shelfhold-sdk` (MIT) n'est publié sur PyPI qu'en **M6**. V1 implémente `Source` et `ExportTarget` ; `MetadataProvider` arrive en M2 ; `Tracker`, `Importer`, `Notifier`, `Processor` sont des noms réservés au contrat esquissé. Les types de données (UnitInfo, PageRef paresseux, ImageRequest, Cursor, FilterSpec, AuthFlow…) sont figés dans ADR-0024, avec compatibilité `api` N-1 par shim.
- **Plugins tiers désactivés par défaut** (SEC-08, EF-SRC-019) jusqu'à l'isolation : activation seulement avec `SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS=1` et confirmation tapée (« mêmes droits que Shelfhold ») ; aucun secret ne leur est transmis. Installation `--only-binary :all: --require-hashes`, sans résolution de dépendances, dans un `site-packages` séparé (ENF-SEC-009). Le détecteur `sys.addaudithook` reste un détecteur, pas une sandbox (EF-SRC-002).
- **Isolation sous-processus JSON-RPC en M6** (EF-SRC-016), avec un `rpc_protocol` versionné distinctement de `api`.
- **Index signé** (A28, EF-SRC-017) : minisign, `version` monotone (anti-rollback), `expires`, sha256 par wheel, révocation, rotation par double signature ; **zéro dépôt préconfiguré** ; l'index officiel **ne liste aucun module de retrait de mesure technique de protection** (ADR-0015).
- **Fetcher V1** = httpx/httpx2 + `httpx-secure` (A13) ; curl_cffi et solveurs passent en M2+ derrière la même garde SSRF. Les deux liens FlareSolverr/Byparr ci-dessous sont des références d'architecture, non des dépendances.
- **Cycle de vie** (EF-SRC-020) : mise à jour, épinglage, retour arrière, désinstallation, liaisons orphelines.

## Exigences liées
EF-SRC-001, EF-SRC-002, EF-SRC-003, EF-SRC-016, EF-SRC-017, EF-SRC-018, EF-SRC-019, EF-SRC-020, EF-SRC-021, EF-SRC-023, ENF-SEC-001, ENF-SEC-009, ENF-QUAL-003, ENF-LEG-001.

## Sources
- https://github.com/extism/extism
- https://github.com/extism/python-pdk
- https://extism.org/docs/concepts/host-functions
- https://github.com/hashicorp/go-plugin
- https://github.com/yt-dlp/yt-dlp/tree/master/.github/workflows
- https://github.com/keiyoushi/extensions-source
- https://github.com/lexiforest/curl_cffi
- https://github.com/FlareSolverr/FlareSolverr
- https://github.com/ThePhaseless/Byparr
- https://github.com/Zaczero/pkgs/tree/main/httpx-secure
- https://github.com/jedisct1/minisign
