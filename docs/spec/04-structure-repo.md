# Shelfhold — Structure du dépôt, workflows et politique de release (v15, après revue passe 13 et arbitrages du porteur 1 et 2)

> Source principale : `research/05-engineering.md` (pratiques d'Immich, Paperless-ngx, Mealie, Home Assistant, keiyoushi, yt-dlp vérifiées le 2026-11-02) et `research/09-closure-B.md` (GitHub, Renovate, SvelteKit 3, Huey). Décisions : ADR-0001, ADR-0005, ADR-0013, ADR-0014, ADR-0016, ADR-0017, ADR-0019, ADR-0022, ADR-0023, ADR-0026, ADR-0028, ADR-0029 ; arbitrages A0, A12, A14, A16–A18, A20–A24, A38 ; passe 2 : B10, B17, B19, B23, B24 ; passe 3 : C1–C4, C12, C14, C17 (ADR-0031 à ADR-0034) ; passe 4 : D1, D2, D3, D12, D13 (ADR-0035 à ADR-0037 ; `review/explorateur-passe4.md` R2, R3, R6) ; passe 6 : F1–F12 (ADR-0040 à ADR-0042 ; règles import-linter, port `ConnectedService`, racines physiques et `adopt_ro`).
> Le dépôt appartient à une **organisation GitHub gratuite** `<org>` (merge queue, transfert de propriété simple, successeur désigné : ENF-GOV-001). Le dossier `research/` n'est **jamais** publié.

---

## 1. Arborescence du monorepo

```
shelfhold/
├── backend/                          # application Python (uv)
│   ├── pyproject.toml                # dépendances (huey>=3.5.4,<3.6, defusedxml …), entry points shelfhold.plugins, ruff/mypy
│   ├── uv.lock
│   ├── alembic/                      # migrations (render_as_batch=True) ; jamais dans une release patch
│   ├── importlinter.ini              # forbidden (domaine sans sqlalchemy/fastapi/httpx) + independence + safe_open_archive unique (ENF-QUAL-002)
│   │                                 # + safe_parse_xml unique : xml.etree, lxml, xml.dom, xml.sax interdits hors infrastructure/xml (ENF-SEC-014)
│   │                                 # passe 9 : xhtml_neutralizer unique (infrastructure/content_trust), seul chemin vers l'export d'un EPUB untrusted (EF-BIB-051, I35)
│   │                                 # passe 6 : (a) aucun module hors plugin_host/adapters/meta/suwayomi (resp. …/lncrawl) et sources/meta_*
│   │                                 #   n'importe un symbole suwayomi/lncrawl ; le cœur ne voit que sdk.connected (EF-SRC-033)
│   │                                 # (b) seul infrastructure/fsops ouvre en écriture, et fsops.write_* refuse toute racine adopt_ro (EF-EXP-060)
│   │                                 # (c) library/replica.py : independent_copies() seul point de décompte des exemplaires (EF-BIB-045)
│   │                                 # (d) catalog/lang.py : normalize_lang() seul producteur de LanguageTag (ENF-I18N-005)
│   ├── src/shelfhold/
│   │   ├── main.py                   # composition root (DI), lifespan, montage SPA
│   │   ├── cli.py                    # shelfhold migrate | admin create-key | admin reset-password | adopt | relink | intents list|resolve | export --dry-run
│   │   │                             #           backup run|verify|restore | export-all | maintenance on|off | secrets rotate | trash empty
│   │   │                             # passe 5 (M1a) : config get|set|list|apply | target add|recheck | source add|recheck | shelf create
│   │   │                             #           adopt --repatriate|--pair-auto | backfill --gaps|--all | detach --restore-upstream | stats
│   │   │                             #           secrets export --passphrase-file (age) ; M2 : revisions list|apply
│   │                             # passe 6 (M1a) : meta rebuild (ENF-OPS-020) | adopt set-follow-from <série> <chapitre> (coverage_unknown) | adopt set-coverage <tome> <premier> <dernier>
│   │                             #           edition set-lang <édition|liaison> <étiquette BCP 47> (language_unknown) | target recheck (racine physique)
│   │   ├── config.py                 # pydantic-settings : env > config.toml > base
│   │   ├── sdk/                      # SDK en V1 (ADR-0024) → publié comme shelfhold-sdk (MIT) en M6 ; passe 11 (GAP-11-15) : sous-arbre sous LICENSE MIT propre
│   │   │   ├── LICENSE               # MIT (le reste du dépôt est AGPL) ; chaque fichier porte « SPDX-License-Identifier: MIT »
│   │   │   ├── age.py                # passe 11 : AgeRating = {min_age: int, x: bool} (EF-EXP-011)
│   │   │   ├── models.py             # SeriesRef, UnitInfo, PageRef (paresseux), ImageRequest, Cursor, FilterSpec, AuthFlow…
│   │   │   ├── source.py  export_target.py  metadata.py          # implémentés (Source, ExportTarget en V1 ; MetadataProvider en M2)
│   │   │   ├── tracker.py importer.py notifier.py processor.py   # noms réservés, contrat esquissé
│   │   │   ├── connected.py          # passe 6 : port ConnectedService et ses capacités (ADR-0041, EF-SRC-033)
│   │   │   ├── lang.py               # passe 6 : type LanguageTag (validé par l'hôte, ADR-0042)
│   │   │   ├── refs.py               # passe 7 : UnitRef, RangeRef, translation_nature (EF-SRC-034, EF-BIB-049, ADR-0024)
│   │   │   ├── wake.py               # port WakeHook (ADR-0025) ; rattachable à toute dépendance, destination comprise (passe 6)
│   │   │   └── testing/              # kit de conformité (plugin pytest)
│   │   ├── catalog/        {domain/, application/, adapters/, lang.py (normalize_lang, données CLDR figées), public.py}
│   │   │                   # Series, Work, Edition (clé avec text_version), Volume, Unit (logique), UnitVariant, Team, WorkRelation, AgeSuggestion,
│   │   │                   # Contributor + ContributorName (passe 6, ADR-0042), dates partielles (EF-BIB-047)
│   │   │                   # domain/invariants.py : invariants I1–I45 (02-architecture §3.1 bis), vérifiés par tests de propriétés
│   │   │                   # domain/normalizers.py : registre normalizer_id@version (number_norm, lang, name, team label, text_version, content_hash), migrations explicites (passe 7, ENF-OPS-021, ADR-0043)
│   │   │                   # domain/coverage.py : unit_coverage, présence d'une Unité (I20, EF-BIB-044, ADR-0040)
│   │   ├── acquisition/    {refresh.py (vérification pilotée), health.py (sonde fonctionnelle), team_policy.py, …, public.py}
│   │   ├── library/        {…, thumbnails/, search/ (FTS5 + trigram), safeguard.py (« Mettre à l'abri »), watermark.py,
│   │   │                    replica.py (registre des exemplaires, independent_copies sur content_hash, passe 6 et 7), content_hash.py (charge utile d'origine, extraction vérifiée de l'original, passe 7, EF-BIB-048), meta_projection.py (JSON régénérables, meta rebuild), public.py}
│   │   ├── export/         {domain/planner.py, domain/naming.py (règles Kavita, EF-EXP-045), domain/path_map.py, application/adopt.py,
│   │   │                    application/intents.py (journal d'intentions), application/locks.py (verrous de ressource, base_version),
│   │   │                    application/foreign_writer.py, application/migration_detect.py (bascule, ADR-0034),
│   │   │                    domain/export_item.py (export_item, chemin figé I22, ADR-0040), domain/export_series.py (ULID propre + edition_export_series, passe 7, EF-EXP-062), domain/physical_roots.py (sonde statfs/st_dev, EF-EXP-059),
│   │   │                    targets/kavita/, public.py}                          # targets/folder, komga, cwa en M4 (transports dropbox/push/pull)
│   │   ├── identity/       {…, visibility.py (portée + classement maximal), activity_view.py, requests.py, public.py}
│   │   ├── integrations/   {apprise/, sablier/, public.py}                      # webhooks/ en M4, trackers/ en M6
│   │   ├── jobs/           {huey_app.py (pools io/cpu/maint), slots.py (domain_slot), progress.py, public.py}
│   │   ├── events/         {bus.py}                                             # outbox.py en M4
│   │   ├── plugin_host/    {registry.py, permissions.py, adapters/{native,meta/{suwayomi,lncrawl}}/, public.py}   # rpc_stdio/ en M6
│   │   │                   # adapters/meta/suwayomi : GraphQL en JSON seulement, liste blanche des routes REST conservées (passe 6, U7)
│   │   ├── interfaces/     {http_api/, sse/, spa/}                              # opds/, kosync/, lite/ en M5
│   │   └── infrastructure/ {fetcher/ (httpx + httpx-secure, cache RFC 9111), db/ (entretien : optimize, checkpoint, vacuum), storage/ (passage | /store),
│   │                        packaging/{cbz,comicinfo,opf_inplace}/, archives/safe_open.py, xml/safe_parse.py (defusedxml),
│                        content_trust/{xhtml_neutralizer.py (xhtml-neutralizer@vN, nh3), cbz_entries.py (liste blanche, octets magiques)} (passe 9, EF-BIB-051),
│   │                        imaging/, fsops/ (openat2 ; racines rw et adopt_ro, copie inter-racines sous intention ; miroir de provenance <racine>/.shelfhold/manifest/, passe 8, EF-EXP-063 ; chemins réservés .shelfhold refusés dans tout FsOp et journal <racine>/.shelfhold/tombstones, passe 9, EF-EXP-001, EF-BIB-052), instance/ (flock /data/.lock + bail par racine physique <racine>/.shelfhold/lease ; restore_epoch et restored_behind, passe 7, EF-INT-026), secrets/ (enveloppe {v, kid, alg, ct} + données associées, rotation reprenable, passe 8), backup/, observability/}
│   └── tests/
│       ├── unit/  property/  integration/  contract/  golden/  faults/   # faults/ = simulateur de pannes (ENF-OPS-011)
│       ├── property/test_model_invariants.py      # invariants I1–I49 (ENF-QUAL-006), kill -9 à chaque étape d'un plan (EF-EXP-041)
│       ├── property/test_coverage_and_frozen_path.py   # passe 6 : aucun cycle ne demande une Unité couverte ; attribution tardive de tome → 0 rename
│       ├── property/test_identity_stability.py    # passe 7 : fusion puis séparation, relink, normaliseur v(n+1), content_hash vs file_hash
│       ├── property/test_tombstone_scopes.py      # passe 10 : portées variant/place/series_binding/work, ligne forgée jamais suppressive (I40, I41, EF-BIB-053)
│       ├── property/test_tombstone_encrypted.py   # passe 11 : journal 2.0 AEAD, ligne liée à racine/portée/seq, dictionnaire de slugs sans effet, trousseau perdu = inerte (I46, EF-BIB-054)
│       ├── property/test_tombstone_migrations.py  # passe 11 : normaliseur v(n+1), content_hash@v2, colonne ajoutée à la clé d'Édition, migrate_ref, fusion (I46)
│       ├── property/test_truth_table_credentials.py  # passe 11 : actions × rôles × crédentials, aucune action réservée par clé ou jeton (I47, EF-USR-024)
│       ├── property/test_tombstone_keyring.py     # passe 12 (M1) : kid cité jamais retiré, rotation sans réécriture du journal, kid inconnu bloquant, racine absente → tombstone_pending_root
│       ├── property/test_upstream_identity.py     # passe 10 : réinstallation simulée de Suwayomi, sourceId + URL, fetch_handle ré-résolu (I42, EF-SRC-036)
│       ├── property/test_variant_identity.py      # passe 8 : nouvel identifiant distant = nouvelle Variante, saisons (number_segment), re-téléversement (I32, I34)
│       ├── fixtures/synthetic/lang_tags/          # passe 6 : étiquettes de langue réelles relevées (formes, pas de contenu) → forme canonique
│       ├── golden/kavita_naming/                  # pièges de nommage Kavita : marqueurs de saison, même numéro, chapitre 0, couverture
│       │                                          # sans préfixe data:, AVIF/JXL — noms SYNTHÉTISÉS, jamais copiés d'une vraie bibliothèque (EF-EXP-045)
│       ├── fixtures/synthetic/malicious_xml/      # XXE fichier, XXE http, billion laughs (ENF-SEC-014)
│       ├── fixtures/synthetic/trapped_epub/       # passe 9 : corpus d'EPUB et de CBZ piégés SYNTHÉTISÉS (script, on*, javascript:, iframe, SVG foreignObject, ressource distante, extension mensongère), aussi livré au kit de conformité (EF-BIB-051)
│       ├── fixtures/synthetic/suwayomi_states/    # source non chargée (bouchon), extension absente, filtres de mise à jour actifs (EF-SRC-026)
│       ├── fixtures/synthetic/<source>/   # HTML/JSON/images SYNTHÉTIQUES ou domaine public (ENF-LEG-002)
│       ├── fixtures/kavita/openapi/<version>.json  # passe 12 (M9) : openapi.json de Kavita figé par version de la plage (minimum 0.9.1, épinglée, dernière), téléchargé au tag puis versionné
│       ├── fixtures/kavita/bootstrap/     # passe 12 (AA6) : amorçage sans interface du Kavita de test (admin créé tant qu'aucun compte n'existe, puis clé dédiée), une fixture par version de la matrice
│       ├── contract/suwayomi-synthetic-extension/  # passe 12 (GAP-12-10) : extension de test synthétique construite dans le job, installée depuis le fichier (jamais d'un dépôt d'extensions)
│       │                                 # + fixtures_server/ : serveur HTTP de fixtures interne au job (CBZ synthétiques), réseau sortant coupé (ENF-LEG-013)
│       ├── fixtures/synthetic/MANIFEST.sha256
│       ├── vectors/koreader_partial_md5/  # vecteurs de hash (ENF-QUAL-005)
│       └── cassettes/<source>/            # VCR, record_mode=none en CI, liste blanche d'en-têtes
├── frontend/                         # SPA SvelteKit 3 + adapter-static + @vite-pwa/sveltekit (ADR-0013)
│   ├── package.json  pnpm-lock.yaml  # versions épinglées
│   ├── src/lib/api/                  # client TS GÉNÉRÉ depuis open-api/ (jamais édité à la main)
│   ├── src/lib/i18n/{fr,en}.json     # FR dès M0, EN avant 1.0 ; messages ICU
│   ├── src/lib/themes/{bibliotheque,console,galerie,calme}/tokens.json  # jetons W3C par thème (ADR-0051) ; A « bibliotheque » par défaut, palette AA (ocre ≈ #A3471F, gris ≈ #6E6357) ; B, C, D en M4
│   ├── src/lib/themes/variants.ts    # liste FERMÉE des points de variante (densité, couvertures, en-tête de série, navigation) ; I51
│   ├── src/lib/fonts/                # polices auto-hébergées
│   ├── src/routes/                   # écrans atelier (V1) ; lecteur en M5
│   ├── vendor/foliate-js/            # sous-module git épinglé (M5), derrière ReaderEngine
│   └── tests/                        # Vitest + Testing Library + MSW
├── sources/                          # sources first-party LÉGALES uniquement (ENF-LEG-001)
│   ├── meta_suwayomi/                # M1a (livrée sans aucune source active)
│   ├── opds/  gutenberg/  wikisource/  standardebooks/  import_folder/  meta_lncrawl/   # M2
│   └── (chaque source : manifest avec terms_url + parse_* purs + tests + fixtures synthétiques)
├── open-api/                         # openapi.json committé + script de génération du client
├── e2e/
│   ├── compose.yaml                  # image du run + serveurs simulés (+ Kavita réel en nightly)
│   ├── fakes/                        # faux Suwayomi, faux OPDS, faux miroir Gutenberg, faux Sablier — synthétiques
│   └── tests/                        # Playwright (dont first_book_under_10_minutes)
├── spikes/                           # code jetable M0 (S1 Huey, S2 SvelteKit 3, S4 RAM) — supprimé après décision
├── docs/                             # documentation PAR PUBLIC (ENF-DOC-001)
│   ├── dependencies.md               # M0 : dépendances critiques, facteur bus, repli, signal (ENF-GOV-002)
│   ├── kavita-routes.md              # passe 12 (M9, M1a) : liste blanche des routes Kavita appelées (route, méthode, usage, jalon) ; test CI contre fixtures/kavita/openapi/
│   ├── mkdocs.yml                    # Zensical ; site publié en M3
│   ├── famille/                      # utiliser, demander, connecter un appareil
│   ├── admin/                        # installer, adopter Kavita, bibliothèques famille, mettre à jour, sauvegarder/restaurer
│   │                                 # (/data ; /store par l'outil de l'hôte), NAS et économie d'énergie, désinstaller, dépanner,
│   │                                 # topologie-type.md, migration-depuis-scripts.md (générique, ADR-0034), redeploiement.md,
│   │                                 # domaine-public-par-pays.md (ADR-0032), achats-tatoues.md (ADR-0033),
│   │                                 # passe 5 : installation-headless.md (EF-INT-024, exemple shelfhold.toml), rapatrier-avant-demontage.md (EF-EXP-051),
│   │                                 # retour-arriere.md (detach --restore-upstream, EF-EXP-056), proprietaire-des-champs.md (Kavita/Shelfhold, EF-EXP-057 ; passe 13 : registre `field_owner`, âge de série à Shelfhold),
│   │                                 # sauvegarde-m1a.md (quotidienne, RPO 24 h, export de clé age), exposition-publique.md (Safe Browsing, ENF-PRIV-002)
│   │                                 # passe 6 : topologie-racines-physiques.md (sous-disques sous /library, sentinelle et staging par racine,
│   │                                 # NFS/CIFS et stabilité des inodes, EF-EXP-059), montage-adopt-ro.md (volume amont en :ro pendant la bascule, EF-EXP-060)
│   ├── plugins/                      # parcours de l'auteur de plugin (EF-SRC-030) : démarrage, contrat, kit de conformité, compatibilité,
│   │                                 # migrations d'état, publication (doc en M3, index en M6) ; conditions des sources
│   ├── legal/cra.md                  # M0 : posture Cyber Resilience Act, critères de bascule (ENF-LEG-011, ADR-0037)
│   ├── governance/{velocite.md, pre-mortem.md, portes.md}   # M0 : vélocité, pré-mortem (ENF-GOV-005, ADR-0035) ;
│   │                                 # portes.md (passe 5) : portes P0–P3, règle d'arrêt, règle de passation (ENF-GOV-006, ADR-0038)
│   ├── recherche/                    # passe 5 : releve-depart.md, synthèse des entretiens et du test concierge (AGRÉGATS seulement ;
│   │                                 # journal nominatif jamais versionné, `docs/recherche/prive/` dans .gitignore, ENF-GOV-006)
│   ├── api/                          # référence OpenAPI, événements
│   ├── reference/config.md           # GÉNÉRÉ depuis config.py : chaque clé avec sa valeur par défaut (N = 6 h, k = 3, 6 mois, plafonds par kind,
│   │                                 # garde disque 5 % / 10 Gio / reprise max(8 %, 15 Gio), sentinelle ≥ 10 et > 5 % ou > 50, fenêtre 03 h–07 h)
│   ├── adr/0001-…md … 0042-…md       # MADR 4 (copie de deliverables/adr)
│   └── security/threat-model.md
├── docker/
│   ├── Dockerfile                    # multi-stage, base python:3.13-slim épinglée par digest, USER 1000:1000, HEALTHCHECK ; passe 12 (AA6) : RLIMIT_CORE=0 (aucun core dump de l'index déchiffré, I46)
│   └── (pas de variante :full publiée en V1 — ENF-PERF-002)
├── compose.yaml                      # compose de référence (= exemple homelab, §5)
├── compose.dev.yaml                  # backend hot reload, Vite, Kavita, serveurs simulés
├── .devcontainer/devcontainer.json
├── .github/
│   ├── workflows/                    # voir §2
│   ├── ISSUE_TEMPLATE/{bug,feature,source-request,config,device-report}.yml   # retirer URL et jetons des logs ; rapport d'appareil (N15)
│   ├── PULL_REQUEST_TEMPLATE.md
│   ├── CODEOWNERS
│   └── renovate.json
├── mise.toml                         # runtimes (python/uv, node/pnpm) + tâches
├── .pre-commit-config.yaml           # exécuté par prek
├── .editorconfig  .dockerignore  .hadolint.yaml  .codecov.yml  typos.toml
├── release-please-config.json  .release-please-manifest.json
├── LICENSE                           # AGPL-3.0-or-later + exception plugin PROVISOIRE dès le premier commit public (ADR-0014, ADR-0036)
├── THIRD_PARTY_NOTICES               # généré en CI
├── README.md  CONTRIBUTING.md  CODE_OF_CONDUCT.md  SECURITY.md  GOVERNANCE.md  TRADEMARKS.md  ROADMAP.md
├── AI_POLICY.md  AGENTS.md
└── CHANGELOG.md                      # géré par release-please
```

Notes :
- Le SDK vit **dans** l'application en V1 (`backend/src/shelfhold/sdk/`) : un seul contrat, compatibilité `api` N-1 via un shim (ADR-0024). Il est extrait et publié sur PyPI sous le nom `shelfhold-sdk` (MIT) en M6, avec la matrice de compatibilité.
- **Frontière de licence du SDK (passe 11, GAP-11-15, ENF-LEG-010, dès le premier commit)** : `sdk/` porte sa propre `LICENSE` MIT et un en-tête `SPDX-License-Identifier: MIT` dans chaque fichier (`test_sdk_files_have_mit_spdx_header`) ; contrat import-linter **« le SDK n'importe jamais le cœur »** (`shelfhold.sdk` ne dépend d'aucun autre module `shelfhold.*`, `test_sdk_never_imports_core`) ; règle de `CONTRIBUTING.md` : aucun code ne passe du cœur AGPL au SDK MIT sans l'accord de ses auteurs (un changement de `sdk/` qui recopie du code du cœur est refusé en revue). L'extraction en paquet séparé en M6 ne demande ainsi aucun audit de provenance.
- **Lint des exportateurs (passe 11, Z3-c, ENF-I18N-003)** : aucun appel `t()` du catalogue i18n dans `exporters/` (les jetons lus par Kavita — `Specials`, `Vol.`, `Ch.` — ne se traduisent jamais) ; aucun âge écrit dans un EPUB (`test_epub_export_writes_no_age_subject_nor_tool_meta`).
- Les sources first-party restent dans le monorepo ; un dépôt séparé `shelfhold-sources` avec index signé n'est envisagé qu'en M6.
- Aucun fichier d'index d'extensions tiers, aucune URL de dépôt tiers, aucun nom de site non autorisé dans le code, la doc ou les gabarits (ADR-0015, ENF-LEG-004).
- **Démos et aperçus de PR** (passe 5, T7) : jamais sur le domaine de la documentation ni sur celui d'une instance ; un domaine distinct, pour qu'un signalement Safe Browsing ne bloque pas les docs.
- **Aucune donnée propre à une installation** (chemins personnels, noms d'hôtes, noms de scripts maison, réseau) n'est versionnée : la doc de bascule est générique ; un plan de déploiement propre à un exploitant vit hors dépôt (`deploiement-*.md` et `PRIVE-*.md` listés dans `.gitignore`, contrôle CI `test_no_private_deployment_doc_in_repo`) (C14).
- **Passe 6 — contrats import-linter ajoutés** (ENF-QUAL-002, ADR-0003) : (a) aucun symbole `suwayomi`/`lncrawl` hors de son adaptateur (EF-SRC-033, ADR-0041) ; (b) `adopt_ro` jamais cible d'écriture de `fsops` (EF-EXP-060) ; (c) `independent_copies()` seul point de décompte des exemplaires (EF-BIB-045) ; (d) `normalize_lang()` seul producteur de `LanguageTag` (ENF-I18N-005). Chacun a aussi un test de comportement (`test_minimal_connected_service_passes_follow_cycle`, `test_adopt_ro_root_never_written`, `test_equivalent_language_tags_same_edition`).
- **Dépendance envisagée passe 6** : `langcodes` (données CLDR) pour `normalize_lang()`, ou une table d'alias figée et versionnée si la dépendance n'est pas retenue ; inscrite dans `docs/dependencies.md` avec ce repli (ADR-0042).
- **Dépendance ajoutée passe 3** : `defusedxml` (analyse XML durcie, ENF-SEC-014), inscrite dans `docs/dependencies.md` avec son repli (lxml durci : `resolve_entities=False`, `no_network=True`, `huge_tree=False`).

---

## 2. Workflows GitHub Actions (par palier, A16)

Toutes les actions sont **épinglées par SHA complet** avec commentaire `# vX.Y.Z` ; `permissions: contents: read` par défaut, élargies par job ; jamais de `pull_request_target` sur du code non fiable.

| Fichier | Palier | Déclencheur | Rôle |
|---|---|---|---|
| `ci.yml` | M0 | `pull_request`, `push` sur `main`, `merge_group` | `changes` (paths-filter) → `lint` (prek : ruff, mypy/pyright, Biome/ESLint, hadolint, actionlint, typos, gitleaks ; import-linter ; **`denylist-names`** avec liste privée lue depuis un secret ; lint de vocabulaire UI à partir de M3) → `backend` (py 3.13 : unit, property, contract, faults ; couverture diff ≥ 85 %, globale ≥ 80 %) → `fixtures` (manifest de hash des fixtures synthétiques, liste blanche d'en-têtes des cassettes) → `frontend` (Vitest, build) → `golden` (CBZ, `xmllint` XSD ComicInfo, pièges de nommage Kavita) → `openapi` (régénère le client TS, échoue si diff) → `image` (matrice native `ubuntu-24.04` / `ubuntu-24.04-arm`, push-by-digest sur `main`, **cache des couches dans le registre** `cache-to: type=registry,ref=ghcr.io/<org>/shelfhold-cache:<arch>,mode=max` plutôt que le cache Actions limité à 10 Go, ENF-QUAL-007) → `e2e` (Playwright sur l'image du run ; axe à partir de M3) → `merge` (`imagetools create`, tags `edge` + `sha-<long>`) → **`ci-success`** (seule vérification requise) ; budget de durée d'une PR ≤ 15 min, mesuré à chaque revue de jalon |
| `dco.yml` | M0 | `pull_request`, `merge_group` | Vérifie la ligne `Signed-off-by:` de chaque commit (DCO 1.1), y compris ceux du mainteneur ; échec = PR bloquée (ENF-LEG-010, ADR-0036) ; agrégé dans `ci-success` |
| `release.yml` | M0 | `push` sur `main` | release-please (PR de release + CHANGELOG avec rubrique « Migration : oui/non, réversible : oui/non ») ; à la release, appelle le workflow image avec tags `X.Y.Z`, `X.Y` ; release immuable |
| `pr-title.yml` | M0 | `pull_request` | Titre en Conventional Commits |
| `nightly.yml` | M1a | `schedule` quotidien | Kavita réel sur la **matrice {minimum 0.9.1, épinglée, `latest`}** (passe 12, M9 ; minimum et épinglée bloquants pour la release, `latest` non bloquant → issue ; Kavita amorcé sans interface par `tests/fixtures/kavita/bootstrap/`) : rangement vérifié par l'API après scan (`test_nightly_real_kavita_places_chapter_volume_from_comicinfo`) ; adoption + export + scan + réinstallation simulée (re-résolution par chemin) ; restauration de sauvegarde sur un autre chemin, sans `/store` ; rescan Grype de la dernière release ; contrat réel de la route de téléchargement Suwayomi (S5) — **passe 11 (L3, ENF-LEG-013)** : Suwayomi avec la **seule source locale** alimentée par les CBZ synthétiques du dépôt, **aucun dépôt d'extensions** (assertion au démarrage du job), **réseau sortant coupé** (réseau Docker `internal: true` après le tirage des images épinglées ; `test_contract_job_has_no_extension_repo_and_no_egress`, `test_contract_suwayomi_uses_local_source_only`) —, de la mutation de rafraîchissement et de `aboutServer`, du chemin GraphQL équivalent et des mutations `follow`/`unfollow` (M1b) ; dérive Suwayomi `latest` (non bloquant) ; **passe 12 (GAP-12-10)** : file de téléchargement réelle jouée par l'extension synthétique contre le serveur de fixtures interne (`test_contract_synthetic_extension_plays_download_queue`) ; E2E « redéploiement = aucune perte » (ENF-OPS-017) ; seconde instance sur les mêmes volumes (ENF-OPS-015) |
| `release.yml` (ajouts, passe 10) | M1a | à la release | Génère et attache `schema.sql` et `data-dictionary.md` (EF-INT-027) ; vérifie les planchers de sécurité du verrou (`test_dependency_floors_enforced_in_lockfile`, ENF-SEC-016 ; passe 11 : `apprise>=2.1,<3`) |
| `ci.yml` (ajouts, passe 11) | M0 / M1a | — | M0 : contrat import-linter « SDK sans cœur » et contrôle des en-têtes SPDX de `sdk/` ; lint « aucun `t()` dans `exporters/` » ; M1a : `test_cited_normalizer_version_never_removed` (registre des normaliseurs cités par une pierre tombale, ENF-OPS-021) |
| `ci.yml` (ajouts, passe 12) | M1a | — | Job hors ligne `kavita-routes` : chaque route de `docs/kavita-routes.md` existe dans `tests/fixtures/kavita/openapi/<version>.json` pour chaque version de la plage (`test_kavita_allowlisted_routes_exist_in_pinned_openapi`) ; `test_cited_tombstone_kid_never_retired` (même principe que le registre des normaliseurs) ; `test_every_unlink_emitter_is_reserved_action` |
| `ci.yml` (ajouts) | M1b | — | Job `scan` (Grype → SARIF) ; attestations **provenance + SBOM** sur les images de `main` et des releases, **attachées aussi en assets de la release** (`sbom.spdx.json`, `provenance.intoto.jsonl`), ce qui les garde hors de GHCR et à l'abri de son nettoyage ; job `merge` qui pousse aussi vers le **second registre** (Q28) avec le même digest |
| `publish-mirror.yml` | M1b | `push` sur `main`, tags `v*` | Pousse le dépôt et les tags vers le **miroir** hors GitHub (Codeberg ou Forgejo personnel) ; échoue sans bloquer `main` (ADR-0029, ENF-GOV-004) |
| `codeql.yml` | M1b | PR, `main`, hebdo | CodeQL Python, JavaScript/TypeScript, `actions` |
| `zizmor.yml` | M1b | PR touchant `.github/` | Lint de sécurité des workflows |
| `release-bench.yml` | M1b | avant release (manuel / PR de release) | Benchmarks sur le profil de référence (ENF-PERF-008) ; régression > 20 % = bloquant release |
| `canary.yml` | M2 | `schedule` nocturne | Tests live **uniquement** des sources dont les conditions l'autorisent ; jamais gutenberg.org ; jamais d'impersonation ni de solveur ; jamais bloquant ; issue `source-broken` |
| `ci.yml` (ajouts) | M3 | — | **cosign** (signature des images de release sur les deux registres), axe bloquant, script de contraste des tokens, E2E chronométré O8 |
| `scorecard.yml` | M3 | `main`, hebdo | OpenSSF Scorecard + badge |
| `docs.yml` | M3 | PR (build) ; `main` (déploiement) | Site Zensical sur GitHub Pages ; référence de config générée |
| `mutation.yml` | facultatif | mensuel | mutmut sur domaine + assainisseurs, non bloquant |
| `cleanup.yml` | M1b | hebdo | Purge des **seuls** tags GHCR `pr-*` de plus de 30 jours ; jamais les digests non étiquetés (manifestes de plate-forme d'une image multi-arch, signatures cosign, attestations), jamais `sha-*`, `edge` ni une version (ENF-QUAL-007, `test_ghcr_cleanup_policy_only_pr_tags`) |
| `publish-sdk.yml` | M6 | tag `sdk-v*` | Publication de `shelfhold-sdk` sur PyPI (et paquet npm éventuel) par **publication de confiance OIDC** : environnement protégé, aucun jeton longue durée, attestations PEP 740 (ENF-SEC-007) |

Retirés par rapport à la v1 du document : Postgres en nightly (ENF-PORT-002 retirée), mutation hebdomadaire obligatoire, benchmarks nocturnes, variante d'image `:full`.

Images publiées : `ghcr.io/<org>/shelfhold:{X.Y.Z, X.Y, edge, sha-<long>}` et, à partir de M1b, les mêmes tags sur le second registre (`<registre2>/<org>/shelfhold`, même digest, même signature) ; avant 1.0 les tags sont `0.Y.Z` / `0.Y`. **Pas de tag `latest` ni de tag majeur seul** : on épingle `X.Y` (A24). Un tag de version n'est jamais réécrit.

---

## 3. Fichiers de gouvernance et d'outillage

| Fichier | Rôle |
|---|---|
| `LICENSE` | AGPL-3.0-or-later ; **amendé passe 4 (D3)** : l'exception plugin (limitée au code dont le projet est titulaire, modèle FSF « controlled interface ») figure **dès le premier commit public**, sous un en-tête « provisoire : texte relu avant 1.0 et avant M6 (Q17) » ; une exception n'étant que permissive, la relecture peut la resserrer avant tout plugin tiers sans revenir vers les contributeurs (ADR-0014, ADR-0036) ; le SDK reste MIT |
| `README.md` | **Statut du projet en première ligne** (passe 5, T7 : `actif`, `maintenance seule` ou `archivé`, avec la date de la dernière revue de porte et un message type « pause annoncée ») ; pitch « l'atelier de la bibliothèque personnelle de votre foyer, pour Kavita » (jamais « partager », ENF-LEG-006), compose de démarrage rapide (avec les deux registres), posture légale en une phrase (« sources légales uniquement ; les fichiers protégés ne sont pas pris en charge »), **lien visible vers `AI_POLICY.md`**, captures sur contenus libres uniquement, lien vers la doc par public |
| `CONTRIBUTING.md` | Règle TDD (test rouge d'abord), Conventional Commits, **e-mail de commit `noreply` GitHub conseillé**, **DCO 1.1 obligatoire** (`git commit -s`, contrôle `dco.yml`), **clause entrante** (« toute contribution est reçue sous AGPL-3.0-or-later avec l'exception de plugin de `LICENSE`, et sous MIT pour `sdk/` » ; passe 11 : aucun code ne passe du cœur au SDK sans l'accord de ses auteurs), pas de CLA (ENF-LEG-010, ADR-0036), fixtures synthétiques uniquement, comment écrire une source (kit de conformité, `terms_url`, **liste d'admission**), **politique de support** (« au mieux », Discussions pour l'aide, Issues pour les bugs reproductibles, `wontfix:scope`, fermeture après 60 jours sans reproduction) |
| `CODE_OF_CONDUCT.md` | Contributor Covenant 2.1 |
| `SECURITY.md` | Politique de divulgation (ENF-SEC-011) : signalement privé GitHub, GHSA, adresse **abus / notifications légales** dédiée dès M0 ; **avant 1.0** (ENF-LEG-011, ADR-0037) : délais (accusé de réception 7 jours, évaluation 30 jours, correctif ou avis 90 jours, au mieux et sans garantie), versions supportées (`X.Y` courante et précédente), procédure d'avis publiée ; contact utilisable par un intégrateur commercial soumis au CRA |
| `ROADMAP.md` | M0 : hypothèse d'heures par semaine (Q34), boîte de temps et estimation par jalon, liste des coupables, règle de coupe à 150 %, vélocité ; « pas de promotion publique avant 1.0 » (ENF-GOV-005, ADR-0035) |
| `docs/governance/portes.md` | Passe 5 (M0, ENF-GOV-006, ADR-0038) : portes P0 (fin de M0), P1 (8 semaines après `0.2`), P2 (avant M3, seuil de 2 demandes par mois → M3-lite), P3 (avant l'annonce de 1.0) ; règle d'arrêt (vélocité < 50 % pendant 3 mois ou deux portes en échec → maintenance seule 12 mois puis archivage) ; règle de passation (≥ 5 PR fusionnées en 6 mois → co-mainteneur) ; relu à chaque revue de jalon |
| `docs/recherche/` | Passe 5 : relevé de départ, synthèses des entretiens et du test concierge, **agrégats seulement** ; tout journal nominatif reste hors du dépôt |
| `docs/legal/cra.md` | M0 : steward ou non, critères de bascule (monétisation, structure porteuse, offre hébergée), obligations de signalement des vulnérabilités activement exploitées (à partir du 11 septembre 2026 pour les fabricants), relecture à chaque revue de jalon (ENF-LEG-011, ADR-0037) |
| `GOVERNANCE.md` | Mainteneur, facteur bus, **successeur désigné** (accès organisation, GHCR, clés de signature, domaine), procédure d'archivage propre (dernière release + export intégral documenté), modération légale des issues, réponse aux notifications (accusé 72 h, pas de contre-notification sans avis juridique, pas de réécriture d'historique), **procédure de continuité** en cas de retrait (bascule vers le second registre et le miroir, ADR-0029), **triage et financement** (aucun don avant un hôte fiscal — Open Collective Europe ou association loi 1901 ; fonctions payantes = Jamais), revue de chevauchement Kavita à chaque jalon, 2FA matérielle (ENF-GOV-001, ENF-GOV-003, ADR-0026) |
| `TRADEMARKS.md` | Les marques citées (Kavita, Komga, Kobo, Kindle…) appartiennent à leurs titulaires ; aucune affiliation (ENF-LEG-004) |
| `AI_POLICY.md` / `AGENTS.md` | Pas de PR autonome d'agent ; **passe 5 (T7)** : la politique couvre aussi le **code assisté par IA du mainteneur** (chaque diff relu, aucun commit de plus d'environ 400 lignes non relu, pas de vitesse au détriment des garanties ni des tests rouges d'abord) ; règles TDD (voir `03-roadmap-tdd.md` §4.5), commandes `mise` ; Shelfhold n'embarque aucun modèle d'IA (à rappeler si un serveur MCP arrive, AI Act, explorateur passe 4 R7) |
| `docs/adr/` | ADR MADR 4 (ADR-0001), 0001 à 0039 |
| `docs/dependencies.md` | Registre des dépendances critiques (M0) : rôle, facteur bus, licence, dernière release, repli, signal de déclenchement ; revue trimestrielle (ENF-GOV-002) |
| `docs/security/threat-model.md` | Modèle de menace (`02-architecture.md`) |
| `.github/CODEOWNERS` | `* @<org>/maintainers` ; `sources/`, `tests/fixtures/`, `tests/cassettes/`, `.github/` explicitement couverts |
| `.github/ISSUE_TEMPLATE/*.yml` | bug (version, module, logs **sans URL ni jeton**), feature, **source-request** (sources légales uniquement ; toute demande visant une source non autorisée est fermée), config, **device-report** (modèle de liseuse, firmware, User-Agent relevé, `/lite` ou SPA, TLS : matrice d'appareils réels, N15) |
| `.github/PULL_REQUEST_TEMPLATE.md` | Cases : test écrit d'abord, exigence liée (`EF-…`/`ENF-…`), docs, migration (oui/non, réversible), fixtures synthétiques, nouvelle dépendance critique inscrite dans `docs/dependencies.md` ; section **« Admission d'une source »** si la PR ajoute une source (preuve d'autorisation ou licence libre, conditions et robots lus, limites, revue CODEOWNERS ; ENF-LEG-003) |
| `renovate.json` | `extends: ["config:best-practices", "security:minimumReleaseAgePypi"]`, **`minimumReleaseAge: "7 days"`** (y compris npm et images), automerge patch/dev après CI verte, épinglage des digests d'actions et d'images, Huey contraint `<3.6`, tableau de bord |
| `release-please-config.json`, `.release-please-manifest.json` | Versionnage de l'application (un composant en V1 ; SDK ajouté comme composant en M6) |
| `.codecov.yml` / `diff-cover` | Patch ≥ 85 %, projet ≥ 80 % |
| `mise.toml` | Runtimes épinglés + tâches `setup, dev, test, test:watch, lint, fmt, e2e, docs, build-image, cassettes:record, bench` |
| `.pre-commit-config.yaml` | Exécuté par prek ; mêmes hooks qu'en CI (sauf `denylist-names`, CI seulement car la liste est privée) |
| `docker/Dockerfile`, `.hadolint.yaml`, `.dockerignore` | Multi-stage, base épinglée par digest, `USER 1000:1000`, `HEALTHCHECK` |
| `THIRD_PARTY_NOTICES` | Généré en CI (foliate-js MIT en M5, etc.) |

Réglages de l'organisation et du dépôt :
- **Rulesets** sur `main` (PR obligatoire, `ci-success` requis, historique linéaire, pas de force-push) et sur les tags `v*` (création réservée à release-please, pas de suppression).
- **Merge queue** activée (disponible car dépôt d'organisation ; Q14 close).
- Signalement privé de vulnérabilités ; releases immuables ; secret scanning + push protection ; politique « actions épinglées par SHA » ; 2FA obligatoire dans l'organisation.

---

## 4. Politique de branches et de releases

| Sujet | Règle |
|---|---|
| Branches | `main` seule branche longue ; branches courtes `feat/…`, `fix/…`, `test/…` ; squash merge via merge queue ; titre de PR en Conventional Commits |
| Commits | Commit rouge séparé recommandé (`test: … (red)`) ; e-mail `noreply` conseillé ; `Signed-off-by:` obligatoire sur chaque commit (DCO, `dco.yml`) dès le premier commit public |
| Versionnage app | SemVer ; `0.x` jusqu'à M3 (`0.1` M0, `0.2` M1a, `0.3` M1b, `0.4` M2) ; **`1.0.0` à la fin de M3**, et aucune promotion publique avant ; dépréciation d'API par en-têtes `Deprecation`/`Sunset`, au moins une mineure et 6 mois après 1.0 ; release-please décide le bump ; le mainteneur choisit quand fusionner la PR de release |
| Versionnage SDK | Interne en V1 (entier `api` + shim N-1) ; publié en M6 avec SemVer indépendant ; support d'une `api` dépréciée pendant 12 mois (à valider, Q20) |
| Tags d'image | `edge` = `main` ; `X.Y.Z` et `X.Y` = releases ; `sha-<long>` = traçabilité ; ni `latest` ni majeur seul ; jamais de réécriture d'un tag de version |
| Migrations | Uniquement en avant, idempotentes ; `shelfhold migrate` (service dédié du compose) ; sauvegarde pré-migration nommée par révision, hors rotation ; **jamais de migration dans une release patch** (contrôle CI) ; Shelfhold **refuse de démarrer** sur un schéma plus récent que le sien (ENF-OPS-002) |
| Retour arrière | Revenir au tag `X.Y` précédent + restaurer la sauvegarde pré-migration ; procédure testée en E2E (ENF-OPS-008) ; notes de version « migration : oui/non, réversible : oui/non » |
| Correctifs | Correctif sur `main` puis release patch ; pas de branche de maintenance avant 1.x ; correctifs de sécurité pour `X.Y` courante et précédente |
| Mises à jour homelab | Compose épinglé sur `X.Y` (ex. `:0.2`) ; auto-update seulement sur ce tag (patchs) ; montée de mineure = action manuelle après lecture des notes |

---

## 5. Compose minimal pour le homelab

Le compose Kavita existant **n'est pas modifié** : Shelfhold monte le **parent existant** des médias Kavita et traduit les chemins (ADR-0022, A20). Shelfhold n'est **jamais** placé derrière Sablier ; il réveille Suwayomi au besoin via le port WakeHook (ADR-0025).

```yaml
# /srv/stacks/shelfhold/compose.yaml
services:
  shelfhold-migrate:
    image: ghcr.io/<org>/shelfhold:0.2
    command: ["shelfhold", "migrate"]
    user: "1000:1000"
    read_only: true
    environment:
      SHELFHOLD_SECRET_KEY_FILE: /run/secrets/shelfhold_secret_key
    secrets: [shelfhold_secret_key]
    volumes:
      - /srv/shelfhold/data:/data
      - /srv/media/shelfhold-store:/store
      - /srv/media/kavita:/library
    restart: "no"

  shelfhold:
    image: ghcr.io/<org>/shelfhold:0.2
    container_name: shelfhold
    depends_on:
      shelfhold-migrate:
        condition: service_completed_successfully
    user: "1000:1000"
    init: true
    read_only: true
    tmpfs: ["/tmp:size=256m"]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    stop_grace_period: 60s
    mem_limit: 768m
    pids_limit: 256
    environment:
      TZ: Europe/Paris
      SHELFHOLD_UMASK: "0002"
      SHELFHOLD_SECRET_KEY_FILE: /run/secrets/shelfhold_secret_key
      # forward-auth désactivé par défaut (arbitrage porteur P5) ; derrière un proxy d'authentification seulement :
      # SHELFHOLD_FORWARD_AUTH: "true"
      # SHELFHOLD_TRUSTED_PROXY_CIDRS: ${PROXY_IP}/32                       # obligatoire si activé
      # SHELFHOLD_PROXY_SECRET_FILE: /run/secrets/shelfhold_proxy_secret    # recommandé
      # Correspondance par défaut (facultative) : /library/manga=/manga,/library/books=/books,/library/comics=/comics
      # SHELFHOLD_PATH_MAP: "/library/manga=/manga;/library/books=/books;/library/comics=/comics"
    secrets: [shelfhold_secret_key]   # + shelfhold_proxy_secret si forward-auth avec secret
    volumes:
      - /srv/shelfhold/data:/data                 # base, config, unit.json, vignettes (SSD conseillé, local, pas NFS)
      - /srv/media/shelfhold-store:/store         # copies conservées seulement (HDD possible) ; même FS que /library → liens durs
      - /srv/media/kavita:/library                # parent EXISTANT des dossiers montés dans Kavita (exemple)
      # staging et corbeille : <racine physique>/.shelfhold/ (même montage → rename atomique, pas d'EXDEV ;
      # un sous-dossier qui est un autre disque reçoit sa propre sentinelle, son staging et sa corbeille, EF-EXP-059)
      # - /srv/media/suwayomi-downloads:/upstream/suwayomi:ro   # BASCULE SEULEMENT : racine adopt_ro, jamais écrite,
      #                                                         # à retirer après le rapatriement (EF-EXP-060, ADR-0034)
      - /srv/backups/shelfhold:/backups           # destination de sauvegarde DISTINCTE (ENF-OPS-001)
    healthcheck:
      test: ["CMD", "shelfhold", "healthcheck"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 60s
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }
    restart: unless-stopped
    networks: [shelfhold_ingress, media]

  # --- optionnel ---
  # suwayomi : service existant, éventuellement géré par Sablier (réveillé par Shelfhold via WakeHook)
  # aucun service de crawler n'est fourni ni activé par défaut (A19)
  # image équivalente sur le second registre : <registre2>/<org>/shelfhold:0.2 (ADR-0029, Q28)

secrets:
  shelfhold_secret_key:
    file: ./secrets/shelfhold_secret_key        # lisible par l'uid 1000 (vérifié au démarrage)
  # shelfhold_proxy_secret:
  #   file: ./secrets/shelfhold_proxy_secret

networks:
  shelfhold_ingress:   # réseau dédié entre le proxy et Shelfhold (recommandé, profil durci ; facultatif sans proxy)
    external: true
  media:               # réseau partagé avec Kavita et Suwayomi
    external: true
```

Côté Kavita : **aucun changement**. Kavita voit toujours `/manga`, `/books`, `/comics` ; Shelfhold écrit sous `/library/manga/…` et appelle le scan Kavita sur le chemin traduit (`/manga/…`). À la première connexion, Shelfhold **adopte** les bibliothèques existantes (EF-EXP-028), dépose le fichier sentinelle `.shelfhold-mount-id` (EF-EXP-029) et ajoute `ExcludePatterns` pour `.shelfhold/`.

Points d'attention :
- **Pas de chown** : les fichiers sont créés par l'uid 1000 avec l'umask configuré ; l'assistant vérifie que l'uid peut écrire et que Kavita peut lire (sonde lue via `plugin/parse` + scan, EF-UX-002) et propose le bloc YAML exact (`group_add`, umask) en cas d'échec.
- `/data` doit être local (NFS/CIFS refusé sauf option explicite, ENF-OPS-010) ; `/store` peut être sur disque dur et doit partager le système de fichiers de `/library` pour les liens durs ; il n'est **pas** dans la sauvegarde de Shelfhold (à confier à l'outil de l'hôte, ADR-0023/0028).
- Contrôle parental : l'assistant propose de créer une bibliothèque Kavita « famille » dédiée par type et rappelle de n'y donner accès qu'aux comptes enfants (ADR-0027) — c'est la seule modification côté Kavita, faite par son API et avec confirmation.
- La clé maîtresse est exportée chiffrée par phrase de passe dans l'assistant ; la sauvegarde va vers `/backups`, distinct de `/data`.
- **Sécurité proportionnée** (arbitrage porteur P1) : le compose ci-dessus active toutes les protections invisibles ; les mesures plus strictes (sessions admin courtes, fraîcheur étendue, expiration obligatoire des clés, secret de proxy, réseau dédié) sont réunies dans `docs/admin/durcir.md` (`SHELFHOLD_SECURITY_PROFILE=hardened`). Aucun proxy n'est supposé : le forward-auth est désactivé par défaut ; une fois activé, `SHELFHOLD_TRUSTED_PROXY_CIDRS` est obligatoire (en `/32`) et le secret recommandé (EF-USR-002). Les **passkeys** (M1b, EF-USR-025) demandent HTTPS ou `localhost`.
- Les plugins tiers sont désactivés par défaut (`SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS` absent).
- **Appareils partagés** (passe 10, ADR-0049, amendée par l'arbitrage porteur P2–P3) : sessions de 30 jours par défaut ; purger, exporter les secrets, vider la corbeille et, dès qu'un profil enfant existe, modifier le contrôle parental redemandent le mot de passe (ou la passkey) après 15 min ; le profil durci rétablit pour l'admin 30 min d'inactivité et 12 h au plus ; `docs/admin/famille.md` recommande un compte Kavita par lecteur sur tout appareil partagé.
- **Pierres tombales** (passe 10, revu passe 11, L1) : le journal `.shelfhold/tombstones` de chaque racine (format 2.0) ne contient que des valeurs **chiffrées** par une clé dérivée du trousseau ; exporter le trousseau (`shelfhold secrets export`) hors de l'hôte, sans quoi, après une perte de `/data` et de la clé, le journal devient **inerte** (il ne bloque plus rien et ne supprime jamais rien : risque accepté, Q64) ; `shelfhold status` le rappelle.
- **Clés d'API** (passe 11, ADR-0049) : l'expiration d'une clé `admin` est facultative (90 jours proposés ; obligatoire dans le profil durci, P4) ; aucune clé ni aucun jeton n'exécute `purge --now`, `tombstone lift`, un changement d'étagère famille ou de clé : passer par l'UI (mot de passe redemandé) ou la CLI de l'hôte.
- **Proxy et tunnels** (passe 11) : ne publier ni `/healthz` ni `/readyz` ; un tunnel public (Funnel ou équivalent) expose l'instance sans forward-auth ; aucun en-tête d'identité du tunnel n'est accepté hors forward-auth configuré (CIDR de confiance, et secret quand il est configuré).
- **Bascule** (ADR-0034) : si Suwayomi écrit aujourd'hui directement dans les dossiers de Kavita, ou si des scripts planifiés renomment, dédoublonnent ou posent les couvertures, suivre `docs/admin/migration-depuis-scripts.md` **avant** d'activer le suivi : retirer les montages directs du compose de Suwayomi, désactiver les scripts pour les séries gérées ; l'assistant refuse le suivi tant que ce n'est pas confirmé.
- **Racines physiques et `adopt_ro`** (passe 6) : si un sous-dossier de `/library` est un autre disque, Shelfhold le détecte (`st_dev`) et y pose sa propre sentinelle ; un disque non monté bloque les écritures vers lui seul. Le volume de téléchargement de Suwayomi n'est monté (en `:ro`) que pendant la bascule, puis retiré (`docs/admin/topologie-racines-physiques.md`, `docs/admin/montage-adopt-ro.md`).
- **Une seule instance** par jeu de volumes : une pile de test ou un `docker run` manuel sur les mêmes `/data` et `/library` refuse de démarrer en écriture (ENF-OPS-015). Passe 12 : la CLI parle au serveur par le socket Unix `/data/shelfhold.sock` (`docker exec … shelfhold …`) ; un `curl` sur `127.0.0.1` n'est jamais traité comme la CLI.
- **Version de Kavita** (passe 12) : **0.9.1 au minimum** pour toutes les étagères ; une version plus ancienne met la destination en mode `degraded_readonly` (aperçu seulement) ; une route de la liste absente (404) la met en `degraded_feature` (`target.route_missing`, sortie `shelfhold target recheck`, passe 13).
- **Trousseau** (passe 12) : refaire `shelfhold secrets export` après chaque rotation ; le kit de secours imprimable ne contient aucune clé, il dit où est rangé ce fichier. Passe 13 : après une **fuite** de la clé, lancer `shelfhold tombstone rekey` (M1b) en plus de la rotation et du changement des identifiants amont.
- **CLI et uid de l'hôte** (passe 13) : le socket de la CLI refuse un processus de l'hôte ou d'un autre conteneur (hors de l'espace de noms PID du conteneur) ; passer par `docker exec`. `SHELFHOLD_CLI_ALLOW_HOST=1` lève ce refus, est journalisé et signalé par `shelfhold status`. Tout processus de l'hôte sous l'uid de Shelfhold vaut un admin : ne pas partager cet uid avec l'utilisateur principal ni monter `/data` dans un autre conteneur.
- **Sources OPDS et destinations** (passe 13) : brancher une source OPDS sur le Kavita ou le Komga où Shelfhold exporte est déconseillé (« utilisez `readback` ») ; les fichiers reconnus par empreinte deviennent des répliques, jamais de nouvelles Variantes.
- **Topologie type** : Shelfhold rejoint le réseau Docker de Suwayomi et l'appelle par son nom de service ; l'assistant teste l'accès GraphQL depuis le conteneur ; les sources natives n'ont pas besoin d'un éventuel nœud de sortie VPN réservé à Suwayomi.
- **Redéploiement** : toute planification vit dans Shelfhold ; aucun cron ni timer sur l'hôte n'est nécessaire (ENF-OPS-017).
