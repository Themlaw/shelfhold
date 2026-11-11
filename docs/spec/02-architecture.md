# Shelfhold — Architecture (v15, révisée passe 13 et arbitrages du porteur 1 et 2)

> Complète `01-cahier-des-charges.md` (exigences EF/ENF). Chaque choix structurant renvoie à un ADR de `adr/`. Les points marqués **[mem]** viennent de la connaissance des chercheurs et non d'une lecture du code : ils sont à reconfirmer avant implémentation. Révision du 2026-11-02 : intègre les arbitrages A0–A38 de la passe 1, **B1–B24 de la passe 2** (qui priment), et les faits vérifiés de `research/08-closure-A.md`, `research/09-closure-B.md` et `research/10-closure-C.md`. Révision passe 3 : arbitrages **C1–C21** (qui priment) ; identité à deux niveaux et invariants (§3.1, §3.1 bis), journal d'intentions, verrous et instance unique (§6.7, ADR-0031), vérification des nouveautés pilotée et sonde fonctionnelle de Suwayomi (§4.1, §6.2), « Mettre à l'abri » (§5.5), autorité de classement et âge effectif (§9 bis), capacité `access_partitioning` (§4.2), XML durci (§5.3), domaine public par pays et fichiers tatoués (§9 quater, ADR-0032, ADR-0033), bascule depuis une automatisation existante (§6.8, ADR-0034), politique de tomes des romans en cours (§6.9). Nouveautés passe 2 : volumes `/data` et `/store` séparés, **mode passage** (§5.5, ADR-0028), **contrôle parental par bibliothèque** et table d'âges (§9 bis, ADR-0027), liens Kavita par chemin relatif (§6.6), discrétion et `visibility`/`activity_view` (§9 ter, ADR-0030), drapeau `irreplaceable`.
>
> Révision passe 4 (2026-11-03) : arbitrages **D1–D13** (`review/ARBITRAGES-passe4.md`, qui priment) ; M1 découpé en M1a et M1b (ADR-0035) ; socle d'authentification et clés d'API hachées dès M1a (ADR-0018 amendée) ; surface d'exploitation (§8) ; texte amont assaini par nh3 (§9) ; copie indépendante, jamais de lien dur, pour toute mise à l'abri, comptage des échecs par source amont limité aux erreurs de contenu et quota (§5.5) ; bail d'instance limité aux sessions d'écriture (§6.7) ; matrice de pannes alignée sur les événements et les codes d'erreur (§11.2). Dans ce document, « M1 » sans suffixe désigne M1a et M1b ; la colonne « Jalon » des modules indique le premier livrable (M1a).
>
> Révision passe 5 (2026-11-03) : arbitrages **E1–E15** (`review/ARBITRAGES-passe5.md`, qui priment) ; configuration headless de M1a (§6.10) ; rapatriement vérifié avant démontage et `detach --restore-upstream` (§6.8) ; nom de série figé, filigrane de suivi, préflight des droits et audit Kavita en lecture seule à l'adoption (§6.3) ; formats sur disque `majeur.mineur` et migrations en avant (§5.6, ADR-0039) ; fenêtre de maintenance de 4 h ordonnancée et interruptible (§6.11) ; Suwayomi multi-comptes (§6.12) ; Kavita second écrivain de métadonnées (§6.13) ; ajouts au modèle (§3.6) ; portes de décision (ADR-0038).
>
> Révision passe 6 (2026-11-03) : arbitrages **F1–F12** (`review/ARBITRAGES-passe6.md`, qui priment) ; couverture « support → Unités » par `export_item` et `unit_coverage` (§3.3, ADR-0040) ; clé d'Édition avec `text_version` (§3.1) ; manifeste clé par `(shelf_destination_id, export_item_id)` et transports de destination (§4.2) ; registre des exemplaires `replica` et `independent_copies(unit)` (§3.3, §5.5) ; racines physiques multiples sous `/library` et racine `adopt_ro` (§5.2) ; chemin exporté figé (§6.14) ; la base fait autorité, arborescence plate par ULID (§5.1) ; port `ConnectedService` (§4.1 bis, ADR-0041) ; langues BCP 47 et contributeurs (§3.1, ADR-0042) ; invariants I20–I24 (§3.1 bis) ; ajouts au modèle (§3.7).
>
> Révision passe 7 (2026-11-03) : arbitrages **G1–G10** (`review/ARBITRAGES-passe7.md`, qui priment) ; empreintes `content_hash` (charge utile d'origine) et `file_hash` (fichier rendu par destination), `/store` qui garde l'original (§3.2, §3.3, §5.1 bis, ADR-0043) ; identité propre d'`export_series` et liaison `edition_export_series` (§3.3) ; Équipe rattachée à l'Édition et `team_alias` (§3.1) ; discriminant de traduction selon sa provenance (§3.1, §4.1) ; époque de restauration et état `restored_behind` (§3.5, §6.7 bis, ADR-0044) ; demande polymorphe d'une Unité ou d'une plage et `download_request_unit` (§3.5, §4.1 bis) ; table `upstream_snapshot` (§3.6) ; normaliseurs versionnés (§3.8) ; position de lecture ancrée sur le support, progression dérivée et bornes de couverture (§3.3, §3.4, ADR-0045) ; invariants I25–I31 (§3.1 bis) ; ajouts au modèle (§3.8).
>
> Révision passe 8 (2026-11-03) : arbitrages **H1–H4** (`review/ARBITRAGES-passe8.md`, qui priment) ; **miroir de provenance par racine physique** dans `.shelfhold/manifest/`, aucune identité dans les fichiers exportés (§5.2, §5.7, ADR-0046) ; règle « nouvel identifiant distant = nouvelle Variante », table `variant_revision` en ajout seul, `supersedes_variant_id`, `numbering_conflict` et `number_segment` dans la place et le filigrane (§3.1) ; `remote_library` à clé locale (§3.3) ; plan d'opérations fichier sérialisable pour les destinations (§4.2) ; enveloppe versionnée des secrets (§9) ; `external_id.id_scheme` (§3.1) ; invariants I32–I34 (§3.1 bis) ; ajouts au modèle (§3.9).
>
> Révision passe 9 (2026-11-03) : arbitrages **J1–J5** (`review/ARBITRAGES-passe9.md`, qui priment) ; **frontière de confiance du contenu** : `content_trust` au manifeste des sources (défaut `untrusted`), dérivé neutralisé `xhtml-neutralizer@vN` des EPUB `untrusted` avant tout export, `derived_hash`, état `extracted_derived`, liste blanche des entrées CBZ (§4.1, §5.8, ADR-0047) ; **effacement immédiat** `shelfhold purge --now`, `content_tombstone` et journal de pierres tombales hors base rejoué après restauration (§5.9, §6.7 bis, ADR-0048) ; `known_bad_versions` par catégorie et blocage des étagères famille sur Kavita ≤ 0.9.0.2 (§9 bis) ; références distantes sans secret (§4.5) ; lignée par entrée de couverture dans le miroir, miroir traité comme entrée non fiable (§5.7) ; incrément du compteur de génération et `fs_intent.epoch` (§6.7 bis) ; `remote_library` rattachée à la bibliothèque du serveur (§3.3) ; chemins réservés refusés dans les plans `FsOp` (§4.2) ; invariants I35–I39 (§3.1 bis) ; ajouts au modèle (§3.10).
>
> Révision passe 10 (2026-11-03) : arbitrages **K1–K9** (`review/ARBITRAGES-passe10.md`, qui priment) ; **portées de pierre tombale** (`variant`, `place`, `series_binding`, `work`) et **journal hors base authentifié** (format 1.1 HMAC et `salt_id`, **remplacé en passe 11 (L1)** par le chiffrement AEAD du format 2.0) (§3.11, §5.9, ADR-0048) ; **identité amont stable** des chapitres Suwayomi (`sourceId` + URL du chapitre, entier local en `fetch_handle`), règle de stabilité du contrat `ConnectedService` et réinstallation simulée au kit, `instance_fingerprint` (§4.1 bis, ADR-0043, ADR-0041) ; règle `access_control` de Kavita étendue à toutes les étagères dès qu'un compte restreint existe (§9 bis, ADR-0027) ; **modèle de session**, fraîcheur d'authentification `fresh_auth_s`, type de problème `reauth-required`, jetons d'appareil au profil lecteur (§3.4, §8, §9 quinquies, ADR-0049) ; `performance` dans la clé d'Édition (§3.1) ; `store/INDEX.jsonl`, `schema.sql` et dictionnaire de données (§5.1 bis, §5.4) ; original `untrusted` gardé dans `/store` en mode passage, tatouage heuristique sans dispense du neutraliseur (§5.8, ADR-0047) ; attestation des valeurs déclarées par un plugin tiers et compléments du contrat de destination (§4.2, ADR-0024, ADR-0011) ; sorties d'une racine durablement différente (§11.2, ADR-0022) ; invariants I40 à I45.
>
> Révision passe 11 (2026-11-03) : arbitrages **L1–L6** (`review/ARBITRAGES-passe11.md`, qui priment) ; **pierres tombales chiffrées** : la table et le journal hors base ne stockent plus d'empreintes salées par l'identifiant d'instance (public) mais les **identifiants bruts chiffrés en AEAD** (XChaCha20-Poly1305, clé HKDF dérivée du trousseau, `kid` par ligne, données associées = format ‖ portée ‖ racine ‖ `seq`), testés en mémoire sous le normaliseur courant **et** chaque version historique citée ; les migrations d'identité consultent les pierres tombales ; trousseau perdu ⇒ journal inerte, risque accepté (§3.12, §5.9, ADR-0048, ADR-0043) ; **axe crédential** de la table de vérité : actions réservées refusées à toute clé d'API et à tout jeton d'appareil, clés `admin` à expiration facultative (arbitrage porteur P4) (§3.12, §9, §9 bis, §9 quinquies, ADR-0049, ADR-0018) ; **banc de contrat Suwayomi** sans contenu tiers (source locale, aucun dépôt d'extensions, réseau sortant coupé ; §6.12, ADR-0016) ; **Édition non classée** jamais admise dans une étagère < 18+ (§9 bis, ADR-0027) ; **âge** stocké en entier minimal + drapeau X, palier 12, jamais écrit dans un EPUB exporté (§3.1, §9 bis) ; réseau (Apprise `>=2.1,<3`, `source.dns_blocked`, `lan_targets` IPv6, en-têtes de tunnel, `/healthz` hors proxy ; §4.5, §8, §9) ; trois classes de priorité Huey (§3.5) ; invariants I46 à I49.
>
> Révision passe 12 (2026-11-03) : arbitrages **M1–M10** (`review/ARBITRAGES-passe12.md`, qui priment) ; **première passe sans manque significatif** (chasseur 0, explorateur 0) : corrections de contradictions et de valeurs seulement. Pierres tombales : `kid` cité jamais retiré, rotation sans réécriture du journal, ligne au `kid` inconnu **bloquante** jusqu'à `secrets import` ou la dérogation « trousseau perdu », données associées liées au **`root_id`** propre à la racine, lignes en attente pour une racine absente (§3.13, §5.9, ADR-0048) ; CLI locale = **socket Unix** `/data/shelfhold.sock` (`SO_PEERCRED`), actions réservées définies par propriété, fraîcheur minimale dès M1a (§9 bis, §9 quinquies, ADR-0049) ; **Kavita minimum unique 0.9.1**, liste des routes `docs/kavita-routes.md` vérifiée contre l'`openapi.json` de chaque version, `parse-bulk`, `plugin/parse` = contrôle du nom seulement (§7, ADR-0020) ; âge de série EPUB poussé verrouillé par l'API Kavita (ADR-0027) ; `?since=` à jeton opaque avec retraits (§8) ; ancre de position polymorphe (ADR-0045).
>
> Révision passe 13 (2026-11-03) : arbitrages **N1–N6** (`review/ARBITRAGES-passe13.md`, qui priment) ; **deuxième passe consécutive sans manque significatif** (chasseur 0, explorateur 0) : **la boucle de revue est close**. Aucun format cassé, aucune identité ni contrat rompu : ajouts additifs (§3.14). Pierres tombales : **registre des `kid` émis** (base et sentinelle), ligne au `kid` absent du registre = forgée ⇒ inerte, ligne au `kid` enregistré mais absent du trousseau ⇒ blocage ; `physical_root` ; `tombstone rekey` après fuite (§5.9). Privilèges : actions réservées « déclenchées par un crédential », liste fermée d'émetteurs système, socket qui refuse un pair hors de l'espace de PID du conteneur (§9). Kavita : registre `field_owner` (§6.13), modes `degraded_readonly`/`degraded_feature`, `target.route_missing` (§11.2). Contrat de destination : `ApiCall.body` (JSON ou référence de contenu), `push` réservé dans le kit jusqu'à M4, Calibre par `/cdb/*` (§4.2, §7). Règle anti-écho (I50).
>
> Révision arbitrage porteur 2 (2026-11-04) : `review/ARBITRAGES-porteur2.md` (Q1–Q3, qui priment) ; Python confirmé (ADR-0002) ; quatre thèmes de front sur un socle unique, A par défaut (§12 bis, ADR-0051, EF-UX-012) ; suivi porté par l'Édition avec liaisons classées et repli (§6.2 étape 10, EF-SRC-037), cadence adaptative (EF-SRC-038), filtres (EF-SRC-039), trous (EF-BIB-056) ; invariants I51 et I52.
>
> Principe directeur : **concevoir large, livrer étroit**. Le modèle et les contrats prévoient dès la V1 les crochets bon marché. Toute machinerie sans usage en V1 est marquée « **réservée (jalon) »** et n'est pas implémentée avant ce jalon.

---

## 1. Vue d'ensemble

Shelfhold est **l'atelier** de la bibliothèque familiale : on y trouve, demande, récupère, range et exporte. **Kavita (et Kover) reste le lieu de lecture** principal de la famille (ADR-0020). En V1, Shelfhold ne sert pas de lecture : OPDS, kosync, restriction d'âge à la lecture et OIDC sont assurés par Kavita. Le lecteur intégré et les surfaces de lecture propres arrivent en M5.

```
   reverse proxy facultatif, au choix (forward-auth désactivé par défaut, P5)   clients V1 : navigateur (UI atelier), CLI
                                  │                                          clients M5+ : KOReader, liseuses (/lite), PWA
┌─────────────────────────────────┼──────────── conteneur shelfhold (Python 3.13, toujours actif) ──────────────────┐
│ interfaces/                     ▼                                                                                 │
│   http_api (REST /api/v1, OpenAPI 3.1)   sse (/api/v1/events)   spa (statique, CSP par hash)   cli `shelfhold`    │
│   origine de contenu dédiée /content/* (CSP sandbox par réponse)                                                  │
│   réservées : opds + kosync + /lite (M5), kobo_sync (M6)                                                          │
│        │ commandes / requêtes (façades public.py uniquement)                                                      │
│ application/ (modules)                                                                                            │
│   catalog ── acquisition ── library ── export ── identity ── integrations ── (reading : M5)                       │
│      └──────────── bus d'événements in-process (table `event` append-only, schema_version) ─────┘                 │
│   jobs : Huey SqliteHuey épinglé + couche Shelfhold (slots par source/domaine, progression → SSE)                 │
│   plugin_host : registre, manifest, `api` N-1 via shim ; adaptateurs natif | méta (V1) ; rpc_stdio (M6)           │
│        │                                                                                                          │
│ infrastructure/ (adaptateurs seulement)                                                                           │
│   fetcher (httpx|httpx2 + httpx-secure, rate-limit, cookies chiffrés)    wakehook (Sablier)                       │
│   db (SQLite WAL, SQLAlchemy 2, Alembic, FTS5 + trigram)   storage (par Unité)   packaging (CBZ, ComicInfo)       │
│   imaging (Pillow/pyvips, plafonds)   fsops (openat2, staging, rename, hardlink opt.)   archives (safe_open)      │
│   secrets   notifications (Apprise)   observability                                                               │
└───────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
     │ HTTP (services tiers installés par l'utilisateur)                             │ fichiers
     ▼                                                                                ▼
  Suwayomi-Server (GraphQL + /api/v1/chapter/{id}/download)                /data     (base, huey.db, config, unit.json, vignettes ; SSD)
  Kavita API (clé admin détenue par le cœur)   Sablier API (réveil)        /library  (parent existant des médias Kavita :
  serveurs OPDS, Gutenberg (catalogues/miroirs), Wikisource (M2)                      manga/ books/ comics/ .shelfhold/)
  instance lncrawl de l'utilisateur (M2)   Apprise URLs                    Kavita voit /manga /books /comics (inchangé)
                                                                           /store    (copies conservées seulement ; HDD possible,
                                                                                      même système de fichiers que /library)
```

Principes : monolithe modulaire hexagonal (ADR-0003) ; Python/FastAPI en bordure (ADR-0002) ; SQLite (ADR-0004) ; les plugins **décrivent**, le cœur **agit** (réseau via Fetcher, fichiers via fsops, appels Kavita via le cœur) ; tout fait métier est un événement versionné.

Chemin V1 (M1a puis M1b, ADR-0020) : **Suwayomi → normalisation → nommage Kavita → écriture atomique → scan ciblé**, plus l'**adoption** de la bibliothèque Kavita existante (EF-EXP-028). L'EPUB est en **passthrough** seulement (M2 : lncrawl, Wikisource, Gutenberg, dossier d'import) ; aucun générateur EPUB maison en V1 (EF-EXP-012 retirée).

---

## 2. Modules

| Module | Responsabilités | Dépendances autorisées | Interdites | Jalon |
|---|---|---|---|---|
| `sdk` (`shelfhold.sdk`, interne au monorepo en V1 ; publié `shelfhold-sdk` MIT en M6) | Contrats Source, ExportTarget (V1), MetadataProvider (M2) ; noms réservés Tracker, Importer, Notifier, Processor ; modèles pydantic ; protocole `Fetcher` ; kit de conformité pytest | stdlib, pydantic | tout module de l'app | M1a |
| `catalog` | Series, Œuvres, Éditions, Tomes, Unités, Variantes, identités externes polymorphes, liaisons de source, alias, dédoublonnage, fusion/séparation, recherche FTS | `sdk`, `jobs.public`, `events` | `export`, `fastapi`, `httpx` | M1a |
| `acquisition` | Suivi (la bibliothèque Suwayomi fait foi pour la liste des séries), vérification des nouveautés **pilotée** (EF-SRC-025), sonde de santé fonctionnelle (EF-SRC-026), filtrage par équipe dès le téléchargement, téléchargement CBZ, intégrité, garde-fou de révision suspecte | `catalog.public`, `library.public`, `plugin_host.public`, `jobs.public`, `events` | `export`, `interfaces` | M1a |
| `library` | `unit.json` pour toute Unité ; copie conservée dans `/store` selon le mode de l'étagère (passage par défaut pour les services connectés, ADR-0028) ; dérivé canonique déclaré, vignettes, corbeille, propriété `irreplaceable`, job « Mettre à l'abri » (EF-BIB-038), drapeau `watermarked` | `infrastructure.storage`, `imaging`, `archives`, `events` | `plugin_host`, `export` | M1a |
| `export` | Étagères, destinations, planificateur, aperçu (avec `base_version`), exécution atomique, **journal d'intentions**, verrous de ressource, manifeste, adoption (détection d'une automatisation existante), sentinelle et bail d'instance, détection d'un autre écrivain, vérification du rangement, plans de renommages | `catalog.public`, `library.public`, `plugin_host.public`, `jobs.public`, `fsops`, `events` | `acquisition` | M1a |
| `identity` | Admin local, profils famille, rôles, clés API scopées, forward-auth avec secret, contrôle parental (portée, classement maximal des étagères, droit « classer »), demandes, **visibilité** et projection unique `activity_view(viewer, event)` (ADR-0030) | `db`, `secrets`, `events` | autres modules applicatifs | M1a (clés) / M3 |
| `integrations` | Consommateurs d'événements : Apprise, résumé quotidien ; webhooks + outbox (M4) ; trackers (M6) ; audit | `events`, `plugin_host.public`, `fetcher` | écriture directe dans `catalog` | M1a |
| `jobs` | Façade sur Huey : files `io`, `cpu`, `maint` (une instance par pool), slots par source/domaine (`domain_slot`), périodiques avec gigue et fenêtres, idempotence, « À vérifier », `flush_locks` au démarrage | `db`, `events` | modules métier | M1a |
| `plugin_host` | Découverte (entry points `shelfhold.plugins`), validation manifest/`api`, shim N-1, état versionné, sous-sources dynamiques ; isolation sous-processus et index signé en M6 | `sdk`, `fetcher`, `secrets` | `catalog`, `export` | M1a |
| `events` | Bus typé in-process, table `event` (append-only, `schema_version`) servant de source au SSE ; outbox persistée **réservée (M4)** | `db` | modules métier | M1a |
| `reading` | Positions par fichier (`reading_position`) et progression dérivée par Unité (passe 7, ADR-0045), appareils, kosync, OPDS, `/lite`, réglages du lecteur | `catalog.public`, `library.public`, `identity.public`, `events` | `acquisition`, `export` | **réservé (M5)** |
| `interfaces/*` | REST, SSE, CLI, SPA, origine de contenu ; mapping DTO ↔ façades ; contrôle parental appliqué via `identity` | façades `public.py` | infrastructure directe, modèles ORM | M1a |
| `infrastructure/*` | Adaptateurs techniques | bibliothèques externes | `application`, `interfaces` | M0+ |

Règles vérifiées par **import-linter** en CI, réduites au strict utile (ENF-QUAL-002 ; passe 6 : aucun symbole `suwayomi`/`lncrawl` hors de son adaptateur, EF-SRC-033 ; `fsops` n'ouvre jamais en écriture une racine `adopt_ro`, EF-EXP-060 ; `independent_copies()` unique point de décompte des exemplaires, EF-BIB-045 ; `normalize_lang()` unique producteur de `LanguageTag`, ENF-I18N-005) : contrat `forbidden` (le domaine n'importe ni `sqlalchemy`, ni `fastapi`, ni `httpx`), contrat `independence` entre modules (communication via `public.py` ou événements), `safe_open_archive()` comme unique point d'ouverture d'archives (ENF-SEC-002) et `safe_parse_xml()` comme unique point d'analyse XML (ENF-SEC-014 : `defusedxml`, aucune entité externe). Pas d'ADR obligatoire pour chaque dépendance inter-modules.

---

## 3. Modèle de données

Identifiants : ULID textuels, **opaques et stables** pour toute entité exposée (API, fichiers sur disque, exports, webhooks M4, `document_alias` M5) ; des entiers peuvent servir de clés internes, jamais exposées (passe 6, F8, ENF-OPS-020). **La base fait autorité** : `unit.json` et `work.json` sont des projections régénérables (§5.1). Horodatages **UTC** en base, horodatage serveur à réception pour tout arbitrage LWW (ENF-TIME-001). Toutes les tables d'état portent `user_id` dès M1a, même si la V1 n'expose qu'un admin et des profils famille (A11). SQLite seul ; pas de SQL spécifique hors adaptateurs (règle de revue, ENF-PORT-002 retirée).

### 3.1 Catalogue

**Définitions (ADR-0021).** L'**Œuvre** (`work`) est la création. La **Série** (`series`) est un regroupement optionnel au-dessus des Œuvres : albums de BD, cycles de romans, intégrales. L'**Édition** (`edition`) est le sextuplet *(œuvre, langue, type de traduction, média, éditeur/officiel, `text_version`)* (passe 6, F2) : `text_version` vaut `''` par défaut (rien ne change pour le manga) et porte, pour la prose (`kind ∈ {novel, light_novel, ebook}`), le traducteur normalisé ou un libellé « abrégé », « jeunesse », « révisé » : deux traductions Gutenberg du même roman sont deux Éditions et deux fichiers ; pour le manga et le webtoon, une Équipe reste une Variante. La langue est une étiquette **BCP 47 canonique** (ENF-I18N-005) ; `all`/`multi`/`und` n'entrent jamais dans la clé. L'**Équipe** de traduction (scanlation) n'est **pas** une Édition : elle se trouve sur la **Variante** d'Unité ; depuis la passe 7 (G3), elle est **rattachée à l'Édition** (`(edition_id, libellé normalisé)`, provenance gardée par `team_alias(source_binding_id, label)`, EF-BIB-035) : aucune fusion entre séries, et un `relink` ne change pas d'équipe. Le choix entre `text_version` et Équipe suit, depuis la passe 7 (G6, EF-BIB-049), la **nature de la traduction** déclarée par la source (`translation_nature`) : une traduction publiée ou de catalogue donne une Édition, une traduction communautaire venue d'un service connecté (y compris un roman web traduit par des équipes qui se relaient) reste une Équipe, quel que soit le `kind` ; `text_version` est dérivé d'une référence (`text_version_contributor_id?` ou `text_version_label`), jamais d'une chaîne de nom. **Identité à deux niveaux (passe 3, C1, ADR-0021 amendée)** : l'**Unité logique** est la place dans l'Édition, `(edition_id, track, number_norm, part)` ; la **Variante** est l'exemplaire fourni par une source et une équipe, `(source_binding_id, remote_unit_id)`. La version passe 2 (« identité d'Unité = liaison + identifiant distant ») ne permettait pas de regrouper les équipes : elle devient l'identité de la Variante. **Passe 8 (H2, EF-BIB-050)** : la place devient `(edition_id, track, number_segment, number_norm, part)` ; `number_segment` vaut `''` par défaut et n'est rempli (par le normaliseur versionné ou par l'admin) que quand une numérotation repart à 1 dans une même Édition, par exemple les saisons d'un webtoon ; un `remote_unit_id` distinct crée **toujours** une nouvelle Variante, une révision ne concerne que le **même** identifiant (`variant_revision`, en ajout seul) et une collision de place entre deux Variantes vivantes d'une même liaison produit la suggestion `numbering_conflict`.

| Entité | Champs clés | Notes / crochets |
|---|---|---|
| `series` | `id`, `title`, `sort_title`, `reading_order` (`publication`/`chronological`/`custom`), `age_rating` (même échelle que `work`), `issn?` (magazine de prépublication, U5) | Optionnelle (EF-BIB-025). À l'export Kavita, la série Kavita est déterminée par `export_series` (§3.3, EF-EXP-032). L'ancienne réservation `content_coverage` (intégrales) est **remplacée par `unit_coverage`** (§3.3, passe 6, F1), qui couvre aussi un fichier contenant plusieurs Œuvres (recueil, omnibus ; réservé) |
| `series_member` | `series_id`, `work_id`, `position` (décimal texte), `label` | Ordre de lecture |
| `work` | `id`, `kind` (FK `work_kind` : manga, manhwa, manhua, webtoon, comic, bd, light_novel, novel, ebook…), `title`, `sort_title`, `description`, `status`, `first_published` (date ISO 8601 partielle + `date_precision`, EF-BIB-047 ; remplace `year`), `age_rating` (passe 11, L5 : **âge minimal entier** `0`, `10`, `12`, `13`, `16`, `18` ; non classé = NULL ; libellés « Tous publics », « 12+ »… par le catalogue i18n ; correspondance ComicInfo §9 bis ; écrit seulement par un compte « classer »), `age_x` (booléen, drapeau X, plus restrictif que tout âge), `rated_at?` (dernier classement, audité ; règle d'I48), `age_effective` (**calculé** : maximum de l'Œuvre et de ses Éditions, EF-BIB-037), `is_adult`, `reading_direction`, `locked_fields[]`, `created_at` | `kind` est un vocabulaire ouvert, sans `audio` : le média audio est porté par l'Édition (EF-BIB-001) |
| `work_kind` | `key`, `label_fr`, `default_shelf`, `default_direction`, `default_media` | Table de référence extensible |
| `work_title` | `work_id`, `title`, `lang` (BCP 47 canonique, ENF-I18N-005), `lang_raw`, `kind` (alt/localized/romanized), `script` | Alimente la recherche FTS et trigram ; romanisation à évaluer (cutlet/pykakasi) (EF-BIB-010) |
| `contributor` | `id` (ULID), `kind` (`person`/`org`), `primary_name`, `birth_date?`, `death_date?` (dates partielles + `date_precision`, EF-BIB-047) | Personne ou organisation **entité** (passe 6, F11, EF-BIB-046) : les dates de vie sont portées par la personne, plus par l'Édition ; `merge_suggestion` s'étend aux contributeurs |
| `contributor_name` | `contributor_id`, `name`, `lang`, `script`, `kind` (`display`/`native`/`romanized`/`sort`), `family_first` (bool) | Formes de nom par langue et écriture ; à l'export, forme d'affichage dans l'ordre naturel de la langue choisie, jamais `sort` dans un champ de personne (I21) |
| `contribution` | `contributor_id`, `entity_type` (series/work/edition/volume), `entity_id`, `role` (MARC relator), `position` | **Polymorphe** (EF-BIB-003) ; ADR-0032 calcule le domaine public sur **toutes** les contributions à droits propres (auteur, illustrateur, traducteur, adaptateur, M2) |
| `external_id` | `entity_type` (series/work/edition/volume/**contributor**), `entity_id`, `provider` (clés stables documentées : `anilist`, `mangaupdates`, `openlibrary`, `isbn13`, `viaf`, `isni`, `bnf`, `wikidata`…), `value` (ISBN-10 converti en ISBN-13), `origin` (source/provider/manual/legacy), `id_scheme?` et `state` (`current`/`superseded`) (passe 8, W3 : même convention que `source_alias`) | Unicité `(entity_type, provider, value)` ; règle « ajouter, ne jamais écraser » : une renumérotation par un fournisseur ajoute la nouvelle valeur et marque l'ancienne `superseded` (EF-BIB-003, M2). Un conflit produit une suggestion, jamais une fusion silencieuse (EF-BIB-003). Wikidata est un **fournisseur de correspondances** optionnel (M4) : un QID connu propose des `external_id` liés, toujours en suggestion ; jamais un pivot d'identité (U5, F12) |
| `edition` | `id`, `work_id`, `lang` (BCP 47 canonique), `lang_raw`, `translation_type` (official/fan/machine/raw), `media` (images/text/file/audio), `publisher` (**non nul**, `''` si inconnu), `text_version` (**non nul**, `''` par défaut, passe 6, F2 ; passe 7 : dérivé de `text_version_contributor_id?` ou de `text_version_label` contrôlé, `text_version_norm_by`, G6, G8), `translated_from_edition_id?` (traduction relais, crochet réservé, U6), `published_on?` + `date_precision` (EF-BIB-047), `is_official`, `preferred`, `shelf_id?`, `age_rating?` (version non censurée, adaptation jeunesse), `rights` (SPDX ou texte + URL d'origine), `rights_basis?` + `rights_jurisdiction?`, `author_death_year?`, `translator?`, `translator_death_year?` (EF-SRC-024 ; depuis la passe 6, **cache dérivé** des `contribution` et des dates de `contributor`, F11), `volume_policy` (`closed_only` défaut / `rolling_tail` / `whole`, EF-BIB-040) | L'Étagère est choisie **par Édition** ; le défaut vient du `kind` (EF-EXP-002) ; `rights` recopié à l'export (EF-BIB-033) ; clé d'unicité sans NULL (GAP-3-25) : `(work_id, lang, translation_type, media, publisher, text_version)` ; passe 8 (GAP-8-03) : `packaging` (sentinelle `''` ; `omnibus`, `kanzenban`, `deluxe`) entre dans la clé en M2, sans changer les clés existantes ; passe 10 (K6) : `performance` (sentinelle `''`, dérivée du rôle `narrator`) entre dans la clé dès le schéma de M1a, pour séparer deux lectures audio d'un même texte |
| `work_relation` | `from_work_id`, `to_work_id`, `kind` (vocabulaire **ouvert**, table de référence comme `work_kind` : `adaptation_of`/`sequel_of`/`prequel_of`/`spin_off_of`, extensible aux relations AniList `side_story`, `alternative`, `summary`… ; passe 6, U6), `origin` | Œuvres liées, jamais fusionnées ; crochet V1, UI et « séries liées » Kavita en M4 (EF-BIB-039) |
| `age_suggestion` | `entity_type` (work/edition), `entity_id`, `value`, `provenance` (comicinfo/opf/sub_source/provider/import), `seen_at`, `state` (open/accepted/rejected) | Âges venus des fichiers, sources et fournisseurs ; jamais un classement (EF-BIB-036) |
| `external_link` | `entity_type`, `entity_id`, `kind` (`official_read`/`buy`/`licensed_in`), `url`, `lang`, `provider`, `fetched_at` | « Soutenir l'auteur » (EF-BIB-032, M4) ; jamais d'affiliation |
| `volume` | `id`, `edition_id`, `number` (texte), `number_sort` (décimal), `title`, `isbn` (ISBN-13 normalisé), `cover_ref`, `closed` (bool, tome clos annoncé par la source ou figé par `volume_policy`, EF-SRC-032) | La liste des Unités d'un tome est portée par `unit_coverage` (§3.3), pas par `unit.volume_id` seul |
| `unit` (Unité logique) | `id`, `edition_id`, `volume_id?`, `number` (texte brut), `number_norm` (texte normalisé ; passe 7 : `number_raw` et `number_norm_by` = `normalizer_id@version`, ENF-OPS-021), `number_sort` (décimal), `number_segment` (passe 8 : `''` par défaut, `number_segment_norm_by`, EF-BIB-050), `part`, `track` (main/annual/special/extra/oneshot/**unnumbered**), `source_order` (place dans le track « non numéroté »), `kind` (chapter/episode/book), `title`, `published_on?` + `date_precision` (date ISO partielle ; `date_upload = 0` → NULL, EF-BIB-047), `published_at` (horodatage de mise en ligne par la source, informatif), `availability` (free/purchased/expiring/locked/stubbed/removed), `available_until`, `state` (known/wanted/queued/stored/exported/adopted/missing/store_missing/corrupt/source_removed/quarantined), `irreplaceable` (**calculé** : aucune liaison active et disponible, `availability=removed`, ou import manuel ; EF-BIB-031), `follow_suppressed` + `follow_suppressed_by`/`_at` (passe 8, GAP-8-06 : « ne plus suivre », jamais levé automatiquement, EF-EXP-018) | Unicité `(edition_id, track, number_segment, number_norm, part)` (EF-BIB-034, ADR-0021 amendée ; `number_segment` à sentinelle `''`, passe 8, EF-BIB-050). Tri total `(track, number_segment, number_sort, part, source_order, published_at)` |
| `unit_variant` (Variante) | `id`, `unit_id`, `source_binding_id`, `remote_unit_id`, `source_item_id`, `team_id?`, `lang` (BCP 47 canonique ; langue du chapitre si la source la donne), `lang_raw`, `revision` (numéro courant, + `revision_hint` ; historique dans `variant_revision`, passe 8), `supersedes_variant_id?` (passe 8 : Variante `removed` qu'elle remplace), `first_seen_at`, `quality_score`, `preferred` | **Unicité `(source_binding_id, remote_unit_id)`** ; passe 8 (EF-BIB-050, I32) : `remote_unit_id` n'est jamais mis à jour, un nouvel identifiant est une nouvelle Variante, une collision de place dans une même liaison ouvre `numbering_conflict` ; règle par défaut : équipe majoritaire déjà présente, puis la plus à jour, puis la plus complète, hors équipes inactives > N mois, repli journalisé ; une Variante exportée par Unité logique (EF-BIB-028) ; UI en M3 (EF-BIB-005) |
| `variant_revision` (passe 8, H2) | `id`, `variant_id`, `revision`, `seen_at`, `page_count`, `content_hash?`, `revision_hint` | **Ajout seul** : jamais de mise à jour ni de suppression ; une révision concerne toujours le même `(source_binding_id, remote_unit_id)` ; lue par le garde-fou EF-BIB-024 (M2) et par `revision_pending` (EF-BIB-043) |
| `team` | `id`, `edition_id`, `label` (normalisé), `label_norm_by`, `last_release_at`, `inactive` (calculé) | **Unicité `(edition_id, label)`** (passe 7, G3 ; avant : `(source_binding_id, label)`) : équipe rattachée à l'Édition, aucune fusion entre séries ; `relink` et les migrations de source la conservent (EF-BIB-035, I27) |
| `team_alias` (passe 7, G3) | `team_id`, `source_binding_id`, `label` (brut, tel que vu sur la liaison), `first_seen_at` | Provenance par liaison de source ; transportée par `relink` (EF-BIB-029) ; une même équipe vue sur deux sources d'une Édition a deux alias et une seule identité |
| `team_priority` | `edition_id`, `team_id`, `rank` | Liste de priorité d'Équipes par Édition (passe 7, G3 ; par Œuvre auparavant) (EF-BIB-028) |
| `source_item` | `id`, `plugin_id`, `source_key` (y compris sous-source, ex. `suwayomi:<sourceId>`), `remote_id`, `url`, `memo` (JSON), `memo_version`, `last_seen` | Identité `(plugin_id, source_key, remote_id)` |
| `sub_source` | `plugin_id`, `source_key`, `name`, `lang` (BCP 47 canonique, ou `mul` pour une sous-source multilingue : jamais recopiée telle quelle dans une Édition), `lang_raw`, `content_warning`, `limit_key`, `enabled` | Sous-sources dynamiques exposées par une méta-source (EF-SRC-021) ; aucune n'est active à l'installation (ENF-LEG-001) |
| `source_alias` | `plugin_id`, `old_source_key`, `old_remote_id`, `new_source_key`, `new_remote_id`, `id_scheme`, `created_at` | Résolution dès le schéma M1a ; migration en masse avec aperçu en M3 (EF-SRC-022) |
| `source_binding` | `id`, `work_id`, `edition_id`, `source_item_id`, `priority` (rang ; porteur 2 : **unique parmi les liaisons actives d'une Édition**), `active`, `disabled_reason?` (porteur 2 : `not_loaded` \| `content_failures` \| `removed_upstream` \| `manual`), `check_cron` et `next_expected` (porteur 2 : **calculés** depuis la cadence observée, EF-SRC-038), `last_checked`, `linked_from_adoption` | N liaisons par Œuvre ; `shelfhold relink` rattache une nouvelle liaison à une Œuvre existante (EF-BIB-029) ; appariement adoption ↔ suivi (EF-EXP-038) ; porteur 2 : plusieurs liaisons classées par Édition, sélection et repli EF-SRC-037 (I52) |
| `merge_suggestion` | `entity_type` (work/contributor ; `variant` en passe 8), `a`, `b`, `score`, `reason` (dont `numbering_conflict`, passe 8, EF-BIB-050), `state` | Dédoublonnage assisté (EF-BIB-004) ; contributeurs depuis la passe 6 (EF-BIB-046) ; collisions de place entre Variantes vivantes d'une même liaison depuis la passe 8 |
| `work_redirect` | `from_work_id`, `to_work_id`, `merge_log_id` | Les anciens identifiants restent résolubles après une fusion (EF-BIB-019) |
| `merge_log` | `id`, `actor_id`, `kind` (merge/split/auto_attach), `before` (JSON), `after` (JSON), `created_at`, `undo_until` (30 j) | Opération « Séparer » et annulation (EF-BIB-019) |
| `metadata_field` | `entity_type`, `entity_id`, `field`, `lang`, `value`, `provider`, `fetched_at`, `locked`, `locked_by` | **Réservée (M2)** avec MetadataProvider. Avant M2 (M1a et M1b), il n'y a que `work.locked_fields` (EF-BIB-008) |
| `collection`, `reading_list`, `reading_list_item` | — | **Réservées (M4)** (EF-BIB-016, EF-USR-020) |

### 3.1 bis Invariants du modèle (ENF-QUAL-006, testés par propriétés Hypothesis)

| # | Invariant | Exigence |
|---|---|---|
| I1 | Une Unité logique est unique par `(edition_id, track, number_segment, number_norm, part)` (passe 8 : `number_segment`, `''` par défaut) ; une Variante est unique par `(source_binding_id, remote_unit_id)` et appartient à exactement une Unité logique | EF-BIB-034 |
| I2 | N équipes × M chapitres numérotés ⇒ M Unités logiques, N×M Variantes, M fichiers exportés | EF-BIB-034, EF-BIB-028 |
| I3 | Au plus **une** Variante exportée par Unité logique et par liaison Étagère × Destination ; jamais deux fichiers de même numéro dans un dossier de série ; une Unité déjà **présente** au sens d'I20 (couverte par un tome adopté) n'est jamais exportée une seconde fois (passe 6) | EF-BIB-028, EF-EXP-045, EF-BIB-044 |
| I4 | Une égalité de place laisse la place à la Variante la plus ancienne (`first_seen_at`), les autres restent Variantes ; entre deux Variantes vivantes d'une **même** liaison, elle produit `numbering_conflict` et rien n'est exporté pour la seconde sans décision (passe 8) | EF-BIB-034, EF-BIB-050 |
| I5 | Une Équipe appartient à une seule Édition ; aucune Équipe n'est partagée entre deux séries ; ses alias de liaison sont transportés par `relink` (passe 7, G3 ; avant : une seule liaison de source) | EF-BIB-035 |
| I6 | `work.age_effective` = maximum des âges des Éditions (et de l'Œuvre) ; NULL si aucun âge validé ; l'âge d'un support qui couvre plusieurs Œuvres est le maximum des Éditions couvertes (passe 7, GAP-7-12, usage réservé) | EF-BIB-037 |
| I7 | Seule une action d'un compte « classer » écrit `age_rating` ; toute autre provenance écrit `age_suggestion` | EF-BIB-036 |
| I8 | Une Œuvre n'appartient à une étagère < 18+ que si `age_effective` est défini et ≤ `shelf.max_age_rating` ; une telle étagère ne vise qu'une destination `access_partitioning ≠ none` | EF-USR-015, EF-EXP-047 |
| I9 | Aucun élément sous intention ouverte (`fs_intent.state ∈ {pending, applying}`) n'est réexporté ni re-planifié | EF-EXP-041 |
| I10 | Au plus une opération disque à la fois par Œuvre et par chemin de série cible ; un plan dont `base_version` est périmée n'est jamais appliqué | EF-EXP-042 |
| I11 | Une purge Suwayomi n'a lieu que si `independent_copies(unit)` compte, hors amont, une copie Kavita vérifiée ∧ (une copie `/store` ∨ une source vivante) ; aucune règle ne recompte les exemplaires autrement (passe 6, F4) | EF-BIB-038, EF-BIB-045 |
| I12 | Une Unité irremplaçable n'est jamais supprimée sans confirmation tapée | EF-BIB-031 |
| I13 | Un tome clos (`volume_policy`) a un hash figé | EF-BIB-040 |
| I14 | Un fichier `watermarked` a toutes ses entrées hors OPF identiques octet pour octet à celles reçues | EF-BIB-041, EF-EXP-033 |
| I15 | La clé d'unicité de l'Édition ne contient aucune colonne nullable | EF-BIB-001 |
| I16 | Une copie conservée ou mise à l'abri a un inode distinct du fichier de `/library` (jamais un lien dur) | EF-BIB-027, EF-BIB-038 |
| I17 | Seules les erreurs de classe `content` incrémentent le compteur qui rend une Unité candidate « irremplaçable » | EF-BIB-042 |
| I18 | Un fichier adopté de même numéro qu'une Unité suivie est la Variante exportée de cette Unité ; il n'est jamais remplacé sans action explicite | EF-EXP-045 |
| I19 | Tout état de `waiting_state` a une commande de sortie | EF-INT-023 |
| I20 | Une Unité logique est **présente** ⇔ une Variante exportée ou un support (`export_item`, `content`, `volume` adopté) qui la couvre (`unit_coverage`) est présent ; le suivi, `backfill --gaps` et I3 raisonnent sur la présence, plus seulement sur le numéro (passe 6, F1) ; deux prédicats (passe 7, GAP-7-11) : `present_for_acquisition(unit)` (au moins un exemplaire lisible) et `present_in(unit, shelf_destination)` (par liaison) | EF-BIB-044, EF-EXP-053 |
| I21 | Aucun nom de personne exporté (ComicInfo) ne contient de virgule (virgules internes remplacées et journalisées ; plusieurs personnes jointes par `, `) ; `sort_name` n'est jamais écrit dans un champ de personne (OPF : `file-as` seulement) (F11) | EF-BIB-046 |
| I22 | **Chemin figé** : le chemin d'un `export_item` est une fonction des seuls attributs figés à sa première exportation (série exportée figée, track, `number_norm`, part, `template_id@version`) ; une attribution tardive de tome ne produit aucun `rename` (F7) | EF-EXP-061 |
| I23 | Toute langue stockée est une étiquette BCP 47 canonique produite par `normalize_lang()` ; `all`/`multi`/`und` n'entrent jamais dans la clé d'Édition (F10) | ENF-I18N-005 |
| I24 | Une racine `adopt_ro` n'est jamais cible d'écriture ; une écriture entre deux racines physiques n'est jamais un `rename` (F5, F6) | EF-EXP-059, EF-EXP-060 |
| I25 | `independent_copies(unit)` compte les exemplaires par `content_hash`, jamais par `file_hash` ; `/store` ne contient que l'original reçu ou un original extrait dont le `content_hash` a été vérifié, jamais un fichier rendu pour une destination ; un rendu n'est jamais copié d'une destination vers une autre (passe 7, G1) | EF-BIB-048, EF-BIB-045 |
| I26 | L'identifiant d'un `export_series` ne change jamais ; une fusion, une séparation ou un rattachement de Série ne modifie que `edition_export_series`, par plan explicite ; aucun `export_series` orphelin ni dupliqué (passe 7, G2) | EF-EXP-062 |
| I27 | Une Équipe est identifiée par `(edition_id, libellé normalisé)` ; un `relink` ou une migration de source ne crée jamais de nouvelle Équipe pour un libellé déjà connu de l'Édition (passe 7, G3) | EF-BIB-035, EF-BIB-029 |
| I28 | Aucune intention créée avant `instance.restore_epoch` n'est rejouée ; en `restored_behind`, aucune écriture, suppression ni scan n'a lieu avant `reconcile --adopt-after-restore` (passe 7, G4) | EF-INT-026, EF-EXP-041, EF-EXP-029 |
| I29 | Toute valeur de clé d'identité porte la version du normaliseur qui l'a produite et sa valeur brute ; aucune clé n'est recalculée par une autre version sans migration explicite (passe 7, G8) | ENF-OPS-021 |
| I30 | Une position de lecture n'est appliquée telle quelle qu'au contenu sur lequel elle a été prise (même support `(carrier_type, carrier_id)` — `export_item` ou, depuis la passe 12 (AA5), `content` pour une Œuvre non exportée lue en copie conservée — et même `content_hash`) ; sinon elle est transposée et marquée `approximate` (passe 7, G9 ; numéro libre suivant, I23 étant la langue) | EF-LEC-014, EF-BIB-026 |
| I31 | Un relevé `upstream_snapshot` n'est jamais purgé tant que la série n'est pas détachée ; le journal d'intentions ne porte que des références (passe 7, G7) | EF-EXP-056, EF-EXP-041 |
| I32 | Un `remote_unit_id` n'est jamais réécrit : un identifiant distant distinct crée une nouvelle Variante ; une révision ne concerne que le même `(source_binding_id, remote_unit_id)` et s'ajoute à `variant_revision` sans rien modifier ; une Variante qui remplace une Variante `removed` porte `supersedes_variant_id`, et cette disparition ne compte pas comme erreur de contenu (passe 8, H2) | EF-BIB-050, EF-BIB-034, EF-BIB-042 |
| I33 | Aucun fichier exporté ne contient d'identité Shelfhold ; la lignée de tout fichier d'une liaison `tree` figure dans le miroir de provenance de sa racine physique, écrit sous intention et clé par `(chemin relatif, file_hash)` ; ce miroir n'est jamais écrit vers une destination `egress = externe` ni placé dans un paquet de diagnostic (passe 8, H1) | EF-EXP-063, EF-EXP-041, EF-EXP-058 |
| I34 | Le filigrane `follow_from` et la place d'une Unité portent le même `number_segment` ; un segment plus récent n'est jamais filtré par le numéro d'un segment antérieur (passe 8, H2) | EF-BIB-050, EF-EXP-053 |
| I35 | Aucun EPUB `untrusted` n'est exporté sans son dérivé neutralisé (`xhtml-neutralizer@vN`, déterministe) ; `content_hash` reste celui de l'original, `derived_hash` et `derived_by` sont enregistrés ; une copie récupérée depuis une destination est `extracted_derived`, jamais `extracted` ; un fichier `publisher` ou `watermarked` n'est jamais réécrit hors de l'OPF ; aucune entrée hors liste blanche n'est exportée dans un CBZ (passe 9, J1) | EF-BIB-051, ENF-SEC-015, EF-BIB-041 |
| I36 | Un contenu sous pierre tombale n'est jamais réacquis, suivi, adopté, réadopté ni réimporté (même référence distante ou même `content_hash`) ; le journal hors base est rejoué avant toute réadoption, y compris en `restored_behind` ; aucune pierre tombale ni entrée d'audit ne garde le titre ; depuis la passe 11 (L1), l'appartenance se teste sur les valeurs brutes déchiffrées, re-normalisées sous chaque version de normaliseur citée (I46) (passe 9, J2) ; depuis la passe 12 (M1, GAP-12-01), « y compris en `restored_behind` » vaut aussi pour une sauvegarde antérieure à une rotation : une ligne au `kid` inconnu bloque au lieu d'être inerte, jusqu'à `secrets import` ou la dérogation tapée « trousseau perdu » ; depuis la passe 13 (N1), seulement si ce `kid` est inscrit au registre des `kid` émis (base ou sentinelle) : sinon la ligne est forgée et inerte | EF-BIB-052, EF-INT-026, EF-BIB-054 |
| I37 | Aucun plan `FsOp` d'une destination ne crée, n'écrit, ne renomme ni ne supprime un chemin réservé (`.shelfhold-mount-id`, `.shelfhold/**`) ; seul l'hôte écrit sentinelle, miroir, staging, corbeille, bail et pierres tombales (passe 9, J5) | EF-EXP-001, EF-EXP-063 |
| I38 | Aucune référence distante stockée (base, `unit.json`, miroir, journaux, sauvegarde) ne contient une valeur secrète connue du Fetcher ni de partie `userinfo` ; une rotation de secret ne change aucun identifiant (passe 9, J5) | EF-SRC-035, EF-BIB-050 |
| I39 | Une session d'écriture incrémente une fois le compteur de génération : commit de la base d'abord, sentinelle ensuite ; une base en avance d'un cran est cohérente ; les comparaisons d'époque utilisent `fs_intent.epoch` (compteur), jamais l'horloge murale (passe 9, J5) | EF-EXP-029, EF-INT-026 |
| I40 | Une pierre tombale bloque selon sa portée : `variant` (référence distante et contenu), `place` (toute nouvelle Variante à la même place), `series_binding` (la série amont n'est ni suivie, ni adoptée, ni recréée), `work` (identifiants externes) ; les valeurs de chaque portée sont stockées brutes et chiffrées (passe 10, K1 ; passe 11, L1) | EF-BIB-053, EF-BIB-052, EF-BIB-054 |
| I41 | Une ligne du journal de pierres tombales qui ne se déchiffre pas sous un `kid` connu (autre racine, ligne altérée ou forgée), dont le `kid` est absent du registre des `kid` émis (ligne forgée, passe 13, N1), ou lue après la dérogation « trousseau perdu », est **inerte** : elle ne bloque ni ne supprime rien et émet `tombstone.unverified` ; une suppression issue d'un rejeu après restauration n'a lieu qu'après confirmation de l'admin (passe 10, K2 ; AEAD et ligne inerte : passe 11, L1, qui remplace « MAC non vérifié = blocage seul ») ; une ligne au `kid` inscrit au registre mais absent du trousseau **bloque** au contraire la réacquisition de sa portée (passe 12, M1 ; passe 13, N1) | EF-BIB-053, EF-BIB-054, EF-INT-026 |
| I42 | `remote_unit_id` et `SeriesRef` d'un service connecté sont des identités de la source amont, stables à la réinstallation de l'outil et à la rotation des secrets ; un identifiant de la base de l'outil n'est qu'un `fetch_handle` en cache (passe 10, K3) | EF-SRC-036, EF-BIB-050 |
| I43 | Sur une instance Kavita de version `access_control` où existe au moins un compte restreint, aucune étagère liée n'est active, quel que soit son classement maximal (passe 10, K4) | EF-EXP-055, ENF-COMP-001 |
| I44 | Une action soumise à la fraîcheur n'aboutit que si `now − auth_time ≤ fresh_auth_s` pour le rôle ; un jeton d'appareil n'a que les droits et filtres de son `subject_user_id`, jamais de son émetteur (passe 10, K5) | EF-USR-022, ENF-SEC-010 |
| I45 | Un EPUB `untrusted` n'échappe jamais au neutraliseur par un tatouage posé par l'heuristique ; son original est gardé dans `/store`, mode passage compris, dès que `/store` existe (passe 10, K9) ; une destination `pull` ne reçoit jamais cet original, seulement un rendu du dérivé (passe 11, GAP-11-13) | EF-BIB-051, EF-BIB-041, EF-BIB-045 |
| I46 | Aucune pierre tombale (table, journal, audit, rapport persistant) ne contient une valeur lisible sans le trousseau : identifiants bruts et `content_hash` sont chiffrés en AEAD, données associées liées à la portée, à la racine et au `seq` ; l'index déchiffré ne vit qu'en mémoire ; un candidat est comparé sous le normaliseur courant **et** chaque version historique citée par une pierre tombale, et une version citée n'est jamais retirée du code ; toute migration d'identité (`normalize migrate`, fusion d'Éditions, `migrate_ref`, alias, `replaced_by`) consulte les pierres tombales (passe 11, L1, GAP-11-01, GAP-11-02) ; un `kid` cité par une pierre tombale n'est jamais retiré du trousseau et la rotation ne réécrit jamais le journal ; les données associées portent le `root_id` de la racine, pas l'`instance_id` (passe 12, M1, M2) ; le **registre des `kid` émis** (base et sentinelle de chaque racine) est la seule source du contrôle « jamais retiré », la table n'est jamais rechiffrée à la rotation, seule `tombstone rekey` réécrit le journal après une fuite, et une ligne au `kid` absent du registre est forgée, donc inerte (passe 13, N1, N5) | EF-BIB-054, ENF-OPS-021, EF-BIB-053 |
| I47 | Une action réservée — définie par propriété depuis la passe 12 (M4) : suppression définitive (`FsOp unlink`, vider une corbeille), effacement de `.shelfhold/`, secrets, contrôle parental, confirmation d'une suppression issue du rejeu — (`purge --now`, `tombstone lift`/`list`, `allowed_profiles`, `max_age_rating`, correspondance profil ↔ Kavita, `root`/`store reinit`, `--accept-admin-risk`, classer, clés et jetons, `secrets export`/`rotate`, démasquage) n'aboutit que par une session — fraîche pour les actions à ré-authentification (arbitrage porteur P3) — ou la CLI locale ; jamais par une clé d'API, portée `admin` comprise, ni par un jeton d'appareil ; l'expiration des clés `admin` est facultative (passe 11, L2, GAP-11-03 ; arbitrage porteur P4) ; la « CLI locale » est le socket Unix `/data/shelfhold.sock` contrôlé par `SO_PEERCRED`, jamais une requête HTTP, loopback compris, et la session doit avoir `auth_time` ≤ 900 s (arbitrage porteur P3) dès M1a (passe 12, M3, M5) ; seules les actions **déclenchées par un crédential** sont concernées : une liste fermée d'émetteurs système (staging orphelin, sonde, corbeille au-delà de 30 j, garde disque), limités aux chemins créés par l'hôte, en est exemptée ; le socket refuse un pair hors de l'espace de noms PID du conteneur (passe 13, N2, N5) | EF-USR-024, ENF-SEC-010, EF-USR-003 |
| I48 | Une Édition non classée (`edition.rated_at` nul, passe 12, GAP-12-09 ; règle appliquée dès M1b aux étagères famille, à `relink` et à l'appariement) ou créée ou rattachée après le dernier classement de son Œuvre (`edition.created_at > work.rated_at`), ou dont un âge suggéré dépasse ce classement, n'appartient à aucune étagère < 18+ tant qu'un compte « classer » ne l'a pas classée (passe 11, L4, GAP-11-05) | EF-BIB-055, EF-BIB-037 |
| I49 | L'âge est un entier minimal plus un drapeau X ; le maximum et les comparaisons sont arithmétiques ; aucun EPUB exporté ne porte d'âge (`dc:subject`) ni de trace de l'outil (`shelfhold:age`) (passe 11, L5, Z3) | EF-EXP-011, EF-EXP-033, EF-BIB-037 |
| I50 | Un fichier reçu d'une source dont le `file_hash` ou le `content_hash` correspond à un export existant (`export_record.file_hash`, anciens hashs de `document_alias`, `content`) est rattaché comme **réplique** de l'`export_item` existant, jamais comme nouvelle Variante : Shelfhold ne réimporte jamais ses propres exports (passe 13, N6, AB4) | EF-SRC-006, EF-BIB-028, EF-EXP-058 |
| I51 | Un thème du front n'est que des jetons de design et des variantes prises dans une liste fermée de points de mise en page : aucun composant, écran ou store ne lit le nom du thème ; aucune logique métier ne dépend du thème (porteur 2, ADR-0051) | `test_no_component_reads_theme_name`, `test_every_theme_tokens_pass_wcag_aa` |
| I52 | Une Unité suivie n'est demandée qu'à **une** liaison par cycle : celle retenue par l'ordre pierres tombales → filtres → préférence d'équipe → meilleur rang actif ; un repli de liaison est toujours notifié (`source.fallback`) (porteur 2, EF-SRC-037) | `test_unit_never_requested_from_two_bindings_same_cycle`, `test_dead_binding_falls_back_with_notification` |

**Limites de test des invariants (passe 12, M10, AA6).** Tous les invariants I1–I50 se testent avec du contenu synthétique, des conteneurs Kavita et Suwayomi (source locale et extension synthétique, ENF-LEG-013), sans contenu tiers ; trois ont une limite écrite : **I42** (stabilité de `sourceId` d'une extension) n'est vérifié que par le contrat et des faux, l'hypothèse étant surveillée en production par `source.renumbered` ; **I43** se teste par une version Kavita **annoncée** simulée, sans faire tourner d'image vulnérable ; **I46** (« l'index déchiffré ne vit qu'en mémoire ») : le test vérifie l'absence d'écriture en base, en fichier et en journal, pas le swap ni les vidages mémoire, d'où `RLIMIT_CORE=0` dans l'image et la recommandation d'un swap chiffré dans la doc admin. I11 et I16 dépendent de la sémantique `(dev, ino)` sondée par racine (`mount_sentinel.capabilities`).



### 3.2 Contenu et stockage

Pas de CAS ni de refcount en V1 (ADR-0010 amendée, EF-BIB-007 retirée). Chaque Unité a son `unit.json` dans `/data/meta` ; son fichier est soit seulement rangé dans `/library` (mode passage), soit conservé dans `/store`, soit référencé (adopté) — ADR-0028.

| Entité | Champs clés | Notes |
|---|---|---|
| `content` | `id`, `unit_id?` (ou couverture par `unit_coverage` pour un fichier qui porte plusieurs Unités), `variant_id`, `kind` (images/text/file), `media_type` (MIME : `application/vnd.comicbook+zip`, `application/epub+zip`, `application/pdf`, `audio/*`…), **0..N par Variante** (une Variante peut porter un EPUB et un PDF ; passe 6, GAP-6-10), `watermarked` (bool, EF-BIB-041), `retention` (`passthrough` : aucun fichier dans `/store` / `keep` / `adopted_ref` : fichier de la bibliothèque référencé), `path?` (relatif à `/store`, seulement si `keep`), `format` (cbz/epub/pdf), `original_sha256` (octets reçus), `content_hash` (charge utile d'origine normalisée, `content_hash()` versionnée ; passe 7, G1, EF-BIB-048), `store_provenance` (`received`/`extracted`), `derived_sha256?`, `derived_by?` (`processor@version`), `size`, `page_count` | Le fichier reçu est conservé. Un dérivé canonique déclaré (ex. désembrouillage) est permis et tracé (EF-BIB-006) |
| `page` | `content_id`, `index`, `sha256`, `width`, `height`, `mime`, `double_spread` | Intégrité (EF-BIB-012) |
| `thumbnail` | `content_hash`, `size` (160/320/640), `format` (webp), `path`, `etag` | Adressée par hash, servie avec `private, immutable` + `Vary: Cookie`, autorisation vérifiée avant le 304 (EF-BIB-020) |
| `trash_item` | `id`, `kind` (unit/work/export_file), `ref`, `path`, `physical_root` (racine physique dont la corbeille `.shelfhold/recycle/` contient le fichier, EF-EXP-059), `deleted_at`, `purge_after` (30 j) | Corbeille (EF-BIB-021) ; « Vider la corbeille » et purge anticipée sous pression disque, jamais d'Unité irremplaçable (EF-BIB-030) |
| `rendition` | — | **Réservée (M5)** : KEPUB, e-ink, conversions (EF-BIB-015) |
| `blob` (CAS + refcount + GC) | — | **Réservé (M4)**, seulement si le fan-out multi-destinations le justifie (EF-BIB-007 retirée) |

### 3.3 Export

Vocabulaire : **Étagère** = ex-Groupe d'export (`shelf`). **Destination** = ex-Cible (`export_target`). La liaison Étagère × Destination remplace l'ancien Binding (`shelf_destination`).

| Entité | Champs clés |
|---|---|
| `export_target` (Destination) | `id`, `kind` (kavita ; folder, komga, cwa en M4…), `name`, `access_partitioning` (`none`/`library`/`user`, lu des capacités, EF-EXP-047), `config` (JSON Schema de la destination ; secrets chiffrés, détenus par le cœur), `path_map` [(local, distant)] (défaut `/library/manga↔/manga`, `/library/books↔/books`, `/library/comics↔/comics`), `version_seen`, `health` |
| `shelf` (Étagère) | `id`, `name` (« Manga »), `phrase` (« Les mangas vont dans Kavita › Manga »), `default_kinds[]`, `format_policy` (cbz_per_chapter ; per_volume en M4), `library_id` (portée de contrôle parental), **`max_age_rating`**, **`allowed_profiles[]`** (ADR-0027), **`retention_mode`** (`passthrough` \| `keep`, ADR-0028), `purge_upstream_after_export` (bool, défaut faux, B6, soumis à EF-BIB-038), `hold_until_rated` (EF-EXP-050), `paused_reason?` (autre écrivain, EF-EXP-043), `follow_suspended[]` (Œuvres d'une bibliothèque enfant adoptée, EF-EXP-048) |
| `shelf_destination` | `id`, `shelf_id`, `target_id`, `transport` (recopié des capacités : `tree`/`dropbox`/`push`/`pull`, EF-EXP-058), `egress` (`foyer`/`externe`), `root_subpath` (**clé stable**), `template` (**copié** à la création), `template_origin` (`default@<version>`), `import_mode` (copy par défaut, reflink si possible ; hardlink en option pour le contenu remplaçable seulement, M2, D9), `kavita_library_type` (Manga/Comic/Book/LightNovel…, passé à `plugin/parse`), `remote_library_id` (passe 8 : clé locale vers `remote_library`, dont `remote_ref` est le **cache** ré-résolu par chemin, EF-EXP-039), `adopted` (bool), `pre_adoption_settings` (JSON : type, `ExcludePatterns`, restauré au détachement), `sync_progress` (M4), `enabled` |
| `export_item` (passe 6, F1) | `id` (ULID), `edition_id`, `kind` (`unit`/`volume`/`bundle`/`adopted`), `frozen_attrs` (JSON : série exportée, track, `number_norm`, part, `template_id@version`, figés à la 1re exportation, I22), `closed` (bool), `content_id?` — Un fichier ou objet exporté, indépendant de la destination ; couvre une ou plusieurs Unités via `unit_coverage` ; en M1a, 1-1 pour les fichiers écrits par Shelfhold (vérifié par invariant), N pour les tomes adoptés ou importés (EF-BIB-044) |
| `unit_coverage` (passe 6, F1) | `carrier_type` (`content`/`export_item`/`volume`), `carrier_id`, `unit_id`, `position` (ordre dans le support), `start_locator?` et `end_locator?` (passe 7, G9 : bornes de l'Unité dans le support, index de page ou href/index de spine, nulles si inconnues, EF-LEC-014), `extent` (`full`/`partial`), `origin` (`comicinfo`/`filename`/`source_name`/`provider`/`manual`), `confidence` — Relation N-N support → Unités ; remplace `covers[]` (M4) et `content_coverage` (V2). En M1a, la plage d'un tome se déduit du préfixe « Vol.N » des noms de chapitres de la source ou d'une saisie manuelle ; ComicInfo et fournisseur en M2. Servira à la correspondance entre Éditions (EF-BIB-026, M4) |
| `replica` (passe 6, F4) | `id`, `unit_id?` / `export_item_id?`, `location` (`upstream`/`library`/`store`/`destination`/`adopted_ro`), `physical_root?`, `rel_path` ou `remote_ref`, `(dev, ino)`, `size`, `mtime`, `content_hash` et `file_hash` (`sha256:<hex>` ; passe 7, G1 : `independent_copies()` compare `content_hash`, EF-BIB-048), `independent` (bool : inode distinct, pas un lien dur), `mutable_by_third_party` (bool : Kavita, KOReader, l'utilisateur peuvent le modifier), `verified_at` — Registre unique des exemplaires (EF-BIB-045) ; lu **seulement** par `independent_copies(unit)` (purge, `irreplaceable`, mise à l'abri, page Santé, fan-out M4) |
| `export_record` (manifeste) | **clé `(shelf_destination_id, export_item_id)`** (passe 6, F3, EF-EXP-058), `locator` typé selon le transport (`path` = `rel_path` pour `tree`, `remote_id` pour une API, `idempotency_key` pour `push`, aucun pour `pull`), `template_id@version` (gabarit qui a produit le nom, R5), `size`, `mtime` (détection d'une modification sur place, EF-EXP-043), `content_hash` (ex-`content_sha256` : charge utile d'origine, passe 7, G1), `sidecar_sha256`, `file_hash` (ex-`exported_file_sha256` : fichier rendu pour cette destination), `format`, `remote_ref` (**cache**, purgé si l'instance Kavita change), `written_by_shelfhold_at`, `exported_at`, `state` (planned/written/scanned/resolved/adopted/missing/recycled/error) ; pour `tree`, `rel_path` reste la **clé de résolution** (index unique `(shelf_destination_id, rel_path)`, EF-EXP-039) ; passe 8 : chaque ligne d'une liaison `tree` est recopiée, avec sa lignée, dans le **miroir de provenance** de sa racine physique (§5.7, EF-EXP-063, I33) |
| `kavita_instance` | `target_id`, `fingerprint` (URL + identifiants et dates de création des bibliothèques), `version`, `seen_at` — détection d'une réinstallation de Kavita (EF-EXP-039) |
| `export_series` (passe 6, GAP-6-12 ; ex-`export_series_name` ; identité propre en passe 7, G2) | `id` (**ULID propre**, attribué à la première exportation, jamais recalculé, EF-EXP-062), `shelf_destination_id`, `remote_library_id`, `proposal_key` (JSON : ancienne clé *(Série ou Œuvre, discriminants d'Édition)*, utilisée seulement pour proposer à la création), `normalized_name` (`name_normalizer`, `name_norm_by`), `name_raw` — **unique** `(remote_library_id, normalized_name)` (EF-EXP-032) ; règle par défaut : si plusieurs Éditions d'une même Œuvre partagent une bibliothèque, la langue suffixe le nom, et `(<lang>, <text_version>)` pour deux Éditions de même langue (passe 7, GAP-7-15) ; série adoptée : valeurs figées (§3.6) ; invariant I26 |
| `remote_library` (passe 8, GAP-8-11 ; rattachée à la bibliothèque du serveur en passe 9, GAP-9-11) | `id` (ULID local), `export_target_id` (passe 9 : unique `(export_target_id, remote_ref)` ; la liaison passe par `shelf_destination_remote_library(shelf_destination_id, remote_library_id)`), `remote_ref` (identifiant du serveur, **cache** ré-résolu par chemin), `kind_seen` (type lu, comparé à `kavita_library_type`, GAP-8-08), `seen_at` — clé de `edition_export_series` et d'`export_series` ; deux serveurs dont les bibliothèques ont le même identifiant distant n'entrent jamais en collision (EF-EXP-062) |
| `edition_export_series` (passe 7, G2) | `edition_id`, `remote_library_id` (FK `remote_library`, passe 8), `export_series_id`, `assigned_at`, `assigned_by` (`first_export`/`plan`) — unique `(edition_id, remote_library_id)` ; une fusion, une séparation ou un rattachement de Série ne change que cette liaison, par réaffectation explicite proposée en plan (EF-EXP-062) |
| `export_plan` / `export_op` | `plan_id`, `dry_run`, `kind` (export/adopt/rename_plan/detach/remove), **`base_version`** + empreinte des fichiers concernés (EF-EXP-042), `batches[]`, ops `{mkdir, write, rename, recycle, delete, touch_mtime, api_call}` ordonnées, `status` |
| `fs_intent` | `id`, `op` (rename/upgrade/recycle/delete/purge_upstream/safeguard/rmdir ; `copy_cross_root` en passe 6), `src_path`, `dst_path`, `src_root`, `dst_root` (racines physiques), `expected_sha256`, `expected_file_id` (ino, taille, mtime ; `dev` seulement pour la comparaison « même racine », GAP-6-13), `plan_id?`, `state` (pending/applying/done/cancelled ; `stale_from_backup` en passe 7 : intention venue d'un instantané, jamais rejouée, EF-INT-026), `created_at`, `resolved_at` — journal d'intentions écrit **avant** l'opération (EF-EXP-041, ADR-0031) |
| `resource_lock` | `resource` (`work:<id>` ou `path:<shelf_destination>/<rel_dir>`), `holder_job_id`, `lease_until` — verrous de ressource (EF-EXP-042) ; libérés au démarrage après la prise du verrou d'instance |
| `mount_sentinel` | **une ligne par racine physique** (passe 6, F5, EF-EXP-059) : `physical_root` (entrée de `PathMap`), `st_dev` vu au démarrage, `capabilities` (stabilité des inodes, reflink, `openat2`), `mount_id` (contenu attendu de `.shelfhold-mount-id` : identifiant d'instance + **compteur de génération** + **`root_id`**, ULID propre à la racine depuis la passe 12, M2, distinct de l'`instance_id`, conservé par `root takeover` et `rebaseline`, lié aux pierres tombales de la racine), `generation`, `checked_at`, `state` (ok/missing/mismatch/stale) (EF-EXP-029) ; une copie divergente (génération en retard) est détectée ; racine absente → écritures refusées vers elle |
| `document_alias` | `exported_file_sha256`, `koreader_partial_md5`, `export_item_id` (passe 7, G9 ; avant : `unit_id`), `content_hash`, `created_at` — une ligne à **chaque réécriture** du fichier exporté (EF-LEC-009, ENF-QUAL-005) ; un hash de tome résout un seul support ; 409 seulement si un même fichier est présent dans deux supports. Table dans le schéma dès M1a, calcul dès M4, utilisation en M5 |

### 3.4 Utilisateurs, demandes, lecture

| Entité | Champs clés | Notes |
|---|---|---|
| `user` | `id`, `username`, `password_hash` (argon2id), `role` (admin/member/child), `can_rate` (vrai pour les admins, EF-BIB-036), `locale`, `timezone`, `title_lang_pref[]`, `theme?` (porteur 2 : `bibliotheque` \| `console` \| `galerie` \| `calme`, nul = défaut de l'instance ; `color_scheme` `auto` \| `light` \| `dark`, EF-UX-012) | Admin local en M1a (EF-USR-021), profils famille en M3 ; `role` et `can_rate` dans le schéma dès M1b |
| `user_policy` | `user_id`, `source_or_plugin`, `mode` (add_direct/request/forbidden) | EF-USR-005 |
| `visibility_scope` | `user_id`, `allowed_libraries[]`, `max_age_rating`, `allow_adult`, `show_unrated` (false pour les profils enfant), `kavita_user?` (correspondance déclarative, EF-USR-017, M3 ; compte Kavita audité, EF-EXP-036), `hide_unrated_remote` (EF-USR-018) | Portée par **bibliothèque**, distincte des étagères. Une seule fonction de requête l'applique (EF-USR-006) |
| `request` | `id`, `requester_id`, `target` polymorphe (`work_id` \| `source_ref` \| `isbn` \| `free_text`, passe 6, GAP-6-17 ; résolu en Œuvre à l'approbation, `pending_source_item` réutilisé dès M3), `edition_hint`, `state` (pending/approved/rejected/not_found/cancelled/arrived ; `to_buy`/`borrow_elsewhere` en M4, EF-USR-019), `source_list_id?` (liste importée, EF-USR-020), `reject_reason`, `age_rating_set`, `size_estimate`, `visibility` (`private`/`household`), `decided_by`, `decided_at` | Demandes (EF-USR-010) ; âge obligatoire à l'approbation d'une demande d'enfant ; visibilité (EF-USR-016) |
| `api_key` | `id`, `user_id`, `prefix` (affichable), `hash` (le secret n'est montré qu'une fois et jamais stocké en clair), `scopes[]`, `read_only`, `expires_at?` (facultative pour toutes les portées, proposée à 90 j ; obligatoire seulement dans le profil durci — passe 11 amendée par l'arbitrage porteur P4), `last_used_at`, `rotated_from?`, `revoked_at`, `subject_user_id` et `issued_by` (M3, passe 11) | Clé admin d'amorçage créée par la CLI en M1a ; `shelfhold admin keys list\|revoke\|rotate` ; audit à chaque création, rotation et révocation (EF-USR-003) ; aucune action réservée par clé (I47, EF-USR-024) |
| `session` | `id`, `user_id`, `csrf_secret`, `created_at`, `last_seen_at`, `auth_time` (dernière saisie du mot de passe, passe 10), `absolute_expiry`, `remember` (faux par défaut, refusé à l'admin sans confirmation), `user_agent_label`, `ip`, `revoked_at` | Session de l'admin dès M1a, révocable par la CLI (EF-USR-021) ; durées bornées par rôle et identifiant renouvelé à la ré-authentification (passe 10, EF-USR-022) ; lister et révoquer dans l'UI en M3 (EF-USR-011) |
| `kavita_account_link` | `user_id`, `target_id`, `kavita_user`, `token` (chiffré) | **Réservée (M4)** (EF-USR-012) |
| `notification_pref` | `user_id`, `mode` (digest/immediate), `digest_hour`, `quiet_hours`, `group_by_series` | Résumé quotidien par défaut (EF-INT-021) |
| `reading_position` (passe 7, G9) | `id`, `user_id`, `export_item_id` (remplacé en passe 12, AA5, par l'ancre polymorphe **`carrier_type ∈ {export_item, content}` + `carrier_id`**, comme `unit_coverage`, colonnes posées dans le schéma M1a), `content_hash`, `locator` (JSON : href, cfi, progression, totalProgression, position, page), `percent`, `client_ts`, `server_ts`, `device_id?` | **Ancre brute** : la position telle que le client l'a envoyée sur le fichier qu'il lisait (Kavita, kosync, lecteur M5) ; historique conservé ; fusion hors ligne par `client_ts` borné par `server_ts`, dates futures rejetées, conflit signalé (EF-LEC-007, ENF-TIME-001) ; invariant I30 (EF-LEC-014). Dans le schéma dès M1a, remplie en M4 et M5 |
| `reading_progress` (vue dérivée, passe 7) | `user_id`, `unit_id`, `state` (`unread`/`in_progress`/`read`), `last_position_id`, `approximate` | **Projection recalculable** de `reading_position` par `unit_coverage` (bornes `start_locator`/`end_locator`) ; lue par le suivi, « Continuer la lecture », le transfert d'Édition (EF-BIB-026) et le renvoi de l'état « lu » vers Suwayomi (M4) ; jamais écrite directement par un client |
| `device_token` (passe 10, K5) | `id`, `subject_user_id` (profil lecteur), `issued_by` (admin), `device_id?`, `scopes[]`, `prefix`, `hash`, `created_at`, `last_used_at`, `revoked_at` | Crochet en M3 (colonnes posées, aucun jeton émis), réalisé en M5 avec EF-USR-008 ; filtres et table de vérité évalués sur `subject_user_id` (I44, EF-USR-022) |
| `webauthn_credential` (porteur 1, P7) | `id`, `user_id`, `credential_id` (unique), `public_key`, `sign_count`, `transports[]`, `aaguid`, `name`, `created_at`, `last_used_at`, `revoked_at?` | M1b (EF-USR-025, ADR-0050) ; clé publique seule, aucun secret en base |
| `device`, `sync_ledger`, `reader_settings`, `annotation` | — | **Réservées** : `device` et `reader_settings` en M5, `sync_ledger` en M6 (Kobo), `annotation` plus tard, ancrée comme `reading_position` sur `(export_item_id, content_hash)` (passe 7, G9) |

### 3.5 Plateforme

| Entité | Champs clés | Notes |
|---|---|---|
| `huey.db` (fichier séparé) | gérée par Huey | Huey `>=3.5.4,<3.6`. Instances `io`, `cpu`, `maint` (EF-INT-003) |
| `job_view` | `id`, `huey_task_id`, `kind`, `status`, `attempts`, `progress`, `dedupe_key`, `source_key`, `limit_key`, `last_error`, `error_code`, `created_at`, `priority_class` (`new` > `request` > `backfill`, passe 11), `request_id?` (« pour qui », passe 11, M1b) | Vue miroir pour l'UI Activité. L'idempotence se fait au chapitre via `dedupe_key`. **Passe 11 (Z2, EF-INT-017, M1b)** : trois classes de priorité Huey fixes (nouveautés suivies > demandes approuvées > rattrapage) ; un rattrapage est découpé en lots par cycle, si bien qu'une nouveauté ou une petite demande passe avant le lot suivant |
| `domain_slot` | `limit_key` (source logique amont ou domaine), `slots`, `in_use`, `lease_until` | Limite de concurrence par source/domaine (le sémaphore Huey exige Redis) ; baux libérés au démarrage (ENF-OPS-007) |
| `schedule` | `id`, `cron`, `jitter_s` (graine propre à l'instance, ENF-LEG-007), `job_kind`, `window` (UTC, rendu local ; fenêtre de maintenance disque, ENF-OPS-014), `enabled`, `last_run_at` | Huey ne rattrape rien (vérifié, closure-C §3) : au démarrage, une tâche de rattrapage enfile **une fois** chaque périodique en retard d'après `last_run_at` ; périodiques idempotentes |
| `instance` | `instance_id`, `jurisdiction` (pays, défaut déduit de la locale, EF-SRC-024), `refresh_mode` (`piloted`/`always_on`, EF-SRC-025), `lease_heartbeat_at`, `restore_epoch` et `restore_generation` (passe 7, G4 : posés par toute restauration ou tout retour arrière, EF-INT-026) | Une ligne ; le bail fichier vit dans `<racine>/.shelfhold/lease`, **un par racine physique** (passe 7, GAP-7-13, ENF-OPS-015) |
| `event` | `id` (séquence monotone), `type` (unit.added, unit.stored, unit.exported, unit.revision_suspect, unit.safeguarded, request.*, backup.*, disk.low, mount.sentinel_missing, target.instance_changed, target.foreign_writer_detected, source.not_loaded, series.stale, resync_required, instance.restored_behind (passe 7), target.library_type_changed (passe 8, M3)…), `schema_version`, `payload`, `occurred_at`, `actor_id`, `visibility` (`private`/`household`/`ops`) | Append-only ; une fonction de **remontée de version à la lecture** par type présente les anciennes lignes au schéma courant (relecture SSE, outbox ; passe 7, V6) ; sert d'identifiant SSE (`Last-Event-ID`) (EF-INT-002) ; purge après 90 j (ENF-OPS-009) ; un identifiant purgé reçoit `resync_required` |
| `outbox_delivery`, `webhook` | — | **Réservées (M4)** : outbox persistée et webhooks HMAC (EF-INT-005) |
| `download_request` | `id`, `target_kind` (`unit`/`range`, passe 7, G5), `variant_id?` ou `range_ref?` (Édition, première et dernière Unité, libellé de tome), `source_binding_id`, `ticket` (opaque, rendu par `server_queue.request`), `state` (requested/downloaded/fetched/cancelled/abandoned), `requested_at`, `attempts` | **Ticket persistant** de la capacité `server_queue` du port `ConnectedService` (passe 6, F9, GAP-6-15), générique Suwayomi et lncrawl ; seuls les chapitres demandés par Shelfhold peuvent être relancés (EF-SRC-026, D7) |
| `download_request_unit` (passe 7, G5) | `download_request_id`, `unit_id`, `state` (`requested`/`covered`/`missing`) | Unités couvertes par un ticket ; la relance (EF-SRC-026) ne porte que sur les Unités demandées non couvertes et le plafond par cycle (EF-EXP-053) se compte en Unités ; à la réponse, un seul `export_item` couvre la plage (EF-SRC-034) |
| `notification_pending` | `id`, `event_id`, `channel`, `attempts`, `next_try_at` | Notification écrite **dans la même transaction** que l'événement, envoyée après validation, purgée après succès (passe 6, GAP-6-23) ; remplacée par l'outbox en M4 |
| `source_failure` | `source_binding_id`, `limit_key` (source amont), `failure_class` (`content` / `auth` / `version` / `schema` / `infra`), `consecutive_content_errors`, `last_error_at` | Comptage par source amont ; seules les erreurs `content` comptent pour « irremplaçable » (EF-BIB-042, D8) |
| `waiting_state` (énumération) | `missing`, `paused_foreign_writer`, `dead_letter`, `instance_changed`, `target_forbidden`, `key_expiring`, `not_indexed`, `auth_expired`, `adoption_refused`, `rating_pending` (M1b), `follow_suspended` (M1b), `safeguard_backlog` (M1b) ; passe 6 : `coverage_unknown` (série adoptée en tomes seuls ou tome de plage inconnue ; sortie `shelfhold adopt set-follow-from <série> <chapitre>`), `language_unknown` (langue `all`/`multi`/`und` sans langue de chapitre ; sortie `shelfhold edition set-lang`) ; passe 7 : `restored_behind` (base en retard sur une sentinelle après restauration ou retour arrière ; lecture seule ; sortie `shelfhold reconcile --adopt-after-restore`, EF-INT-026) ; passe 8 : `library_type_changed` (M3 ; type de bibliothèque Kavita modifié dans Kavita ; sortie `shelfhold target reconnect`, EF-EXP-049) | Chaque valeur a une route et une commande de sortie (EF-INT-023, D5) |
| `plugin_install` | `plugin_id`, `version`, `api`, `origin` (builtin/local ; repo en M6), `sha256`, `enabled`, `state_schema` | Plugins tiers désactivés par défaut (EF-SRC-019) |
| `plugin_state` | `plugin_id`, `key`, `payload`, `payload_schema` | État versionné (EF-SRC-001) |
| `plugin_repo` | `url`, `pubkey`, `last_version` (monotone), `expires` | **Réservée (M6)** (EF-SRC-017) |
| `source_config` | `plugin_id`, `settings` (chiffré si secret), `cookie_jar` (chiffré), `user_agent`, `rate_limit_override`, `terms_url`, `health` (dont dernier téléchargement réussi, taux d'échec, fraîcheur, EF-SRC-026), `consecutive_failures` (EF-BIB-038), `rights_basis` (EF-SRC-024), `family_safe` (bool, marquée « famille » par l'admin, EF-USR-014) | EF-SRC-013, ENF-LEG-003 |
| `source_account` | `user_id`, `plugin_id` (capacité `auth_mode` du port `ConnectedService`), `auth_kind`, `secret` (chiffré), `session_state` (`expires_at`) | AuthFlow par utilisateur (EF-SRC-023) |
| `provider_quota` | `provider`, `bucket_capacity`, `refill_per_s`, `daily_cap`, `tokens`, `reset_at` | Seau à jetons par fournisseur de métadonnées (ENF-LEG-005) |
| `http_cache` | `key`, `etag`, `last_modified`, `body_path`, `expires_at` | |
| `setting` | `key`, `value` | Niveau 3 de la configuration (env > `config.toml` > base, ENF-OPS-004) |
| `audit` | `actor_id` (jeton opaque après suppression de l'utilisateur, EF-USR-013), `action`, `target`, `at` | Approbations, **classements** (EF-BIB-036), visibilité, **démasquage** d'une activité privée (EF-USR-016), plugins (ENF-OBS-003) ; rétention 1 an |

### 3.6 Ajouts de la passe 5 au modèle (E1–E15)

| Table / champ | Contenu | Exigence |
|---|---|---|
| `export_series` (ex-`export_series_name`, étendue ; renommée passe 6) | Pour une série adoptée : `kavita_series_id` en cache, `frozen_series`, `frozen_localized_series`, `frozen_series_sort`, `frozen_language`, `has_volume`, `series_dir`, `frozen_at` ; relu avant chaque export, jamais recalculé depuis la source | EF-EXP-052, EF-EXP-057 |
| `series_follow` | `edition_id` (porteur 2 : le suivi porte sur l'**Édition**, plus sur une liaison ; dès le schéma M1a), `work_id`, `follow_from` (filigrane ; passe 8 : couple `(number_segment, number_norm)`, I34, EF-BIB-050), `backfill_mode` (`none` \| `gaps` \| `all`), plafond par cycle, `upgrade_policy` (`keep_first` \| `prefer_rank`, porteur 2), `check_override?` (« terminée », « en pause », EF-SRC-038), `filters` (EF-SRC-039) ; table annexe `gap_ack(edition_id, track, number_segment, number_norm, acked_by, acked_at)` (EF-BIB-056) | EF-EXP-053 |
| `waiting_state` (valeurs ajoutées) | `permission_denied`, `child_exposure`, `auth_mode_unsupported` | EF-EXP-054, EF-EXP-055, EF-SRC-031, EF-INT-023 |
| `fs_intent` (étendue) | `op = repatriate` ; `intent_schema_version` gelé (une intention d'un format inconnu n'est jamais rejouée) | EF-EXP-051, ENF-OPS-019 |
| `upstream_snapshot` | Réglages Suwayomi relevés à l'adoption (catégorie, réglage de mise à jour de la bibliothèque) pour `detach --restore-upstream` ; **table dédiée** depuis la passe 7 (G7) : `connected_service_id`, `series_ref`, `capability`, `payload`, `schema_version`, `taken_at`, écrite par la capacité `upstream_restore`, sauvegardée, sans rétention automatique, jamais purgée sans détachement (I31) ; le journal d'intentions ne porte que l'opération de restauration | EF-EXP-056, EF-SRC-033 |
| `kavita_access_audit` | Résultat du dernier audit en lecture seule : bibliothèque, comptes ayant accès, restriction, « inclure les inconnus », `exposed_to_children`, date | EF-EXP-055 |
| `source_account` | `kind` (`admin` \| `service`), `auth_mode` lu (`basic_auth`, `ui_login`, …) ; en M1b, compte de service Suwayomi dédié | EF-SRC-031 |
| `revision_pending` | Révisions accumulées avant M2 : `variant_id`, nature (`metadata_only` \| `rewrite`), impact KOReader estimé, état | EF-BIB-043 |
| Vue `v_value_metrics` | Agrégats calculés sur les tables existantes (attentes, rejeux, exports, délai détection → indexation, gardes déclenchées) ; aucun nouveau stockage | EF-INT-025 |

### 3.7 Ajouts de la passe 6 au modèle (F1–F12)

Tous ces ajouts sont dans le **schéma de M1a** : ce sont des tables d'association, des clés et des déclarations, presque sans code, qui deviendraient des migrations de sens une fois le manifeste rempli.

| Table / champ | Contenu | Exigence |
|---|---|---|
| `export_item`, `unit_coverage` | Support exporté indépendant de la destination ; relation N-N support → Unités (`full`/`partial`, origine, confiance) ; remplace `covers[]` et `content_coverage` ; I20 | EF-BIB-044, ADR-0040 |
| `export_record` (re-clé) | Clé `(shelf_destination_id, export_item_id)` + `locator` typé ; `rel_path` clé de résolution pour `tree` | EF-EXP-058, EF-EXP-039 |
| `replica` + `independent_copies(unit)` | Registre unique des exemplaires ; fonction unique de décompte | EF-BIB-045, ADR-0010 |
| `edition.text_version`, `edition.translated_from_edition_id?` | Discriminant de traduction ou de version du texte dans la clé ; crochet de traduction relais | EF-BIB-001, ADR-0021 |
| `lang` + `lang_raw` (partout) | Étiquette BCP 47 canonique + valeur reçue et provenance | ENF-I18N-005, ADR-0042 |
| `contributor`, `contributor_name`, `external_id.entity_type ∋ contributor` | Personne ou organisation, formes de nom, identifiants, dates de vie | EF-BIB-046, ADR-0042, ADR-0032 |
| `published_on` + `date_precision`, `work.first_published`, `contributor.birth_date/death_date` | Dates ISO partielles ; jamais de précision inventée | EF-BIB-047 |
| `mount_sentinel` (par racine physique), `trash_item.physical_root`, `fs_intent.src_root/dst_root`, `op = copy_cross_root` | Racines physiques multiples sous `/library` | EF-EXP-059, ADR-0022 |
| `PathMap` : `mode` (`rw` \| `adopt_ro`) | Racine en lecture seule pour l'adoption et le rapatriement | EF-EXP-060, ADR-0022 |
| `export_item.frozen_attrs` | Attributs figés à la première exportation (chemin figé, I22) | EF-EXP-061, ADR-0040 |
| `export_series` | Correspondance explicite entités Shelfhold ↔ série distante | EF-EXP-032 |
| `content.media_type`, 0..N par Variante ; capacité `accepted_content_types` | Plusieurs formats d'un même titre ; la destination déclare ses préférences | EF-EXP-058, EF-EXP-046 |
| `download_request.ticket` | Ticket persistant de `server_queue` | EF-SRC-033, ADR-0041 |
| `request.target` polymorphe | Demande vers un résultat distant non matérialisé (M3) | EF-USR-010 |
| `notification_pending` | Notifications d'exploitation persistées avant l'outbox de M4 | EF-INT-006 |
| `waiting_state` (valeurs ajoutées) | `coverage_unknown`, `language_unknown` | EF-EXP-053, ENF-I18N-005, EF-INT-023 |
| Identifiants | ULID opaques pour toute entité exposée ; la base fait autorité sur les JSON | ENF-OPS-020, ADR-0039, ADR-0009 |

### 3.8 Ajouts de la passe 7 au modèle (G1–G10)

Comme en passe 6, tout est dans le **schéma de M1a** (colonnes, tables d'association, déclarations de contrat) ; les tables de lecture restent vides jusqu'en M4. Les tests correspondants sont écrits avant le code (`03-roadmap-tdd.md`, M1a, bloc « Passe 7 »).

| Table / champ | Contenu | Exigence |
|---|---|---|
| `content_hash` / `file_hash` (`content`, `replica`, `export_record`, `unit.json`), `content.store_provenance` | Charge utile d'origine (fonction unique versionnée) et fichier rendu par destination ; `/store` = original reçu ou extrait vérifié | EF-BIB-048, ADR-0043, ADR-0010 |
| `export_series.id` (ULID propre), `edition_export_series` | Identité de la série distante indépendante des fusions ; liaison réaffectée par plan | EF-EXP-062, ADR-0043, ADR-0040 |
| `team.edition_id`, `team_alias`, `team_priority.edition_id` | Équipe à l'échelle de l'Édition, provenance par liaison | EF-BIB-035, EF-BIB-028, EF-BIB-029, ADR-0043, ADR-0021 |
| `instance.restore_epoch`, `restore_generation`, `fs_intent.state = stale_from_backup`, `waiting_state = restored_behind` | Époque de restauration, intentions d'instantané jamais rejouées, lecture seule jusqu'à la réadoption | EF-INT-026, ADR-0044, ADR-0031 |
| `download_request.target_kind`, `range_ref`, `download_request_unit` | Demande d'une Unité ou d'une plage, Unités couvertes par ticket | EF-SRC-034, ADR-0041 |
| `edition.text_version_contributor_id?`, `text_version_label` ; `translation_nature` (contrat) | `text_version` dérivé d'une référence ; discriminant selon la provenance | EF-BIB-049, ADR-0043, ADR-0021 |
| `upstream_snapshot` (table dédiée) | Relevé durable des réglages amont | EF-EXP-056, ADR-0044 |
| `*_norm_by` (`normalizer_id@version`) + `number_raw`, `label_raw`, `name_raw` ; table de référence `normalizer(id, version, introduced_in)` | Normaliseurs versionnés, migration explicite | ENF-OPS-021, ADR-0043, ADR-0039 |
| `reading_position`, `reading_progress` (vue dérivée), `document_alias.export_item_id`, `unit_coverage.start_locator?`/`end_locator?` | Position par fichier, progression par Unité recalculable, bornes de couverture | EF-LEC-014, ADR-0045, ADR-0040, ADR-0012 |
| `shelf_destination.egress` attesté (valeur par défaut du plugin) ; règle de liaison `pull` | Egress par instance ; une destination `pull` exige un exemplaire lisible | EF-EXP-058, ADR-0011 |

### 3.9 Ajouts de la passe 8 au modèle (H1–H4)

Même règle qu'en passes 6 et 7 : tout ce qui touche une identité ou un format sur disque est dans le **schéma de M1a** ; le reste est daté par son jalon. Tests écrits avant le code (`03-roadmap-tdd.md`, M1a, bloc « Passe 8 »).

| Table / champ / format | Contenu | Exigence | Jalon |
|---|---|---|---|
| Miroir de provenance `<racine>/.shelfhold/manifest/` (format versionné) | Lignée par `(chemin relatif, file_hash)` : `export_item`, couverture et bornes, Variante (`source_key`, sous-source, `remote_unit_id`), `content_hash`, Équipe, gabarit, Édition, série exportée ; projection de la base, écrite sous intention | EF-EXP-063, ADR-0046, ADR-0039 | M1a |
| `unit.number_segment` (`''`), `number_segment_norm_by` ; `series_follow.follow_from = (number_segment, number_norm)` | Segment de numérotation dans la place et le filigrane | EF-BIB-050, EF-EXP-053, ADR-0021, ADR-0043 | M1a |
| `variant_revision` (ajout seul), `unit_variant.supersedes_variant_id?`, `merge_suggestion.reason = numbering_conflict` | Révision à identifiant égal ; remplacement d'une Variante disparue ; collision de place signalée | EF-BIB-050, EF-BIB-034 | M1a |
| `remote_library(id, export_target_id, remote_ref, kind_seen)` (passe 9 : clé par bibliothèque du serveur, liaison `shelf_destination_remote_library`) | Bibliothèque distante à clé locale | EF-EXP-062, EF-EXP-049 | M1a (table) / M3 (relecture du type) |
| `unit.follow_suppressed` + provenance | « Ne plus suivre » persistant | EF-EXP-018 | M1a |
| Enveloppe des secrets `{v, kid, alg, ct}` + données associées `(table, colonne, id)` | Chiffré versionné, identifiant de clé, secret non transposable d'une ligne à l'autre, rotation reprenable | ENF-SEC-005 | M1a |
| Règle de liaison : deux liaisons `tree` aux racines qui se recouvrent → refus `target.tree_overlap` | En attendant l'ADR « arbre et indexeurs » (Q48) | EF-EXP-058 | M1a |
| `FsOp` (plan d'opérations sérialisable des destinations) | Remplace l'objet vivant `f` | EF-EXP-001, ADR-0024, ADR-0011 | M3 (avant la doc auteur) |
| `external_id.id_scheme?`, `external_id.state` | Renumérotation par un fournisseur sans écrasement | EF-BIB-003, ADR-0043 | M2 |
| `edition.packaging` (sentinelle `''`) | Réédition reconditionnée chez le même éditeur | EF-BIB-001 | M2 |
| `unit_coverage.carrier_type = unit` | Chapitre re-découpé couvert par d'autres Unités | EF-BIB-044 | M2 |
| OPF et ComicInfo d'origine dans `/data/meta` (référencés par `unit.json`) | Restauration de l'original pour `store_provenance = extracted` | EF-BIB-048 | M2 |
| `team.number_offset` | Décalage de numérotation entre équipes, contrôle avant repli | EF-BIB-028 | M3 |
| `TargetCapabilities.series_grouping` (`name`/`folder`) | Regroupement des séries propre à la destination | EF-EXP-032 | M4 |
| `plugin_install.signer_pubkey`, `index_url` | Clé de plugin épinglée à son signataire | EF-SRC-017 | M6 |

### 3.10 Ajouts de la passe 9 au modèle (J1–J5)

Même règle qu'aux passes 6 à 8 : tout ce qui est un **contrat** ou une **identité** est posé en M1a, même quand le code qui s'en sert arrive plus tard (le champ `content_trust` est dans le contrat SDK de M1a, le neutraliseur en M2). Tests écrits avant le code (`03-roadmap-tdd.md`, bloc « Passe 9 »).

| Table / champ / format | Contenu | Exigence | Jalon |
|---|---|---|---|
| `SourceManifest.content_trust` (`publisher` / `untrusted`, défaut `untrusted`) ; `source_binding.content_trust` (recopié, surchargeable vers `untrusted` seulement par l'admin) | Confiance déclarée dans le **corps** des fichiers | EF-BIB-051, EF-SRC-001, ADR-0047 | M1a |
| `content.derived_hash?`, `content.derived_by?` (`xhtml-neutralizer@vN`), `replica.fidelity` (`original` / `derived`) ; `store_provenance ∈ {received, extracted, extracted_derived}` | Dérivé neutralisé ; exemplaire lisible mais non original | EF-BIB-051, EF-BIB-006, EF-BIB-048, EF-BIB-045 | M1a (colonnes) / M2 (neutraliseur) |
| Liste blanche des entrées CBZ + concordance extension / octets magiques ; code `content.untrusted_entry` | Refus en M1a, rastérisation des SVG en M2 | EF-BIB-051, ENF-SEC-004 | M1a / M2 |
| `content_tombstone(id, ref_digest, content_hash, derived_hash?, reason_code, created_at, epoch)` ; journal `<racine>/.shelfhold/tombstones` (JSON Lines en ajout seul, `schema_version`) | Effacement immédiat, blocage de la réacquisition, rejoué après restauration | EF-BIB-052, ADR-0048, ADR-0044 | M1a |
| Miroir de provenance v1.1 : lignée **par entrée de couverture**, `content_hash_by`, `derived_hash`/`derived_by`, descripteur de liaison, clé d'Édition, état `unresolved` | Lignée fidèle pour les supports à plage et les séries nées après la sauvegarde | EF-EXP-063, ADR-0046 | M1a |
| `fs_intent.epoch` ; règle d'incrément de la génération (commit, puis sentinelle) | Époques comparées par compteur | EF-INT-026, EF-EXP-029, ADR-0044, ADR-0022 | M1a |
| `unit.number_segment_ordinal` | Ordre des segments (`S10` après `S2`) | EF-BIB-050 | M1a |
| `remote_library.export_target_id` + `shelf_destination_remote_library` | Unicité et audit par bibliothèque du serveur | EF-EXP-062, EF-EXP-055 | M1a (schéma) / M1b (audit famille) |
| Trousseau : clés retirées conservées (`kid`, `retired_at`) | Copie de `/data` antérieure à une rotation déchiffrable | ENF-SEC-005 | M1a |
| `known_bad_versions[].category` (`cve`, `access_control`) | Blocage des étagères famille sur une faille de contrôle d'accès | ENF-COMP-001, EF-EXP-055, ADR-0027 | M1a (données) / M1b (blocage) |
| ComicInfo d'origine de Suwayomi dans `/data/meta` pour les candidates « irremplaçable » | Mise à l'abri depuis `/library` avec métadonnées d'origine | EF-BIB-048 | M1b |
| Gabarit de référence `{secret}` des adaptateurs ; rédaction des segments de chemin | Références sans secret | EF-SRC-035 | M2 |
| `upstream_restore` : schéma de `payload` sans secret | Relevé amont sauvegardable | EF-EXP-056 | M2 |
| Liste des chemins réservés validée dans tout plan `FsOp` | Sentinelle, miroir et pierres tombales hors d'atteinte des destinations | EF-EXP-001 | M3 (avant la doc auteur) |

---

### 3.11 Ajouts de la passe 10 au modèle (K1–K9)

Même règle qu'aux passes 6 à 9 : tout ce qui est un **format sur disque**, une **identité** ou un **contrat** est posé en M1a ; le reste est daté par son jalon. Tests écrits avant le code (`03-roadmap-tdd.md`, blocs « Passe 10 »).

| Table / champ / format | Contenu | Exigence | Jalon |
|---|---|---|---|
| `content_tombstone.scope` (`variant`, `place`, `series_binding`, `work`) ; `scope_digest` et `salt_id` **remplacés en passe 11 (L1)** par les valeurs brutes chiffrées (§3.12) | Pierre tombale par portée ; une Œuvre purgée bloque aussi sa série amont | EF-BIB-053, ADR-0048 | M1a (`work` : M2) |
| Journal `<racine>/.shelfhold/tombstones` format 1.1 (`scope`, `kid`, `salt_id`, `mac` HMAC-SHA256) — **remplacé en passe 11 (L1)**, avant toute écriture réelle, par le format **2.0** chiffré (§3.12) | Journal authentifié ; ligne non vérifiée : blocage seul en passe 10, **inerte** depuis la passe 11 | EF-BIB-053, EF-BIB-054, ENF-SEC-005, ADR-0039 | M1a |
| `unit_variant.remote_unit_id` = identité amont (`suwayomi:<sourceId>` + `chapter.url` normalisé) ; `unit_variant.fetch_handle?` + `fetch_handle_seen_at` | Identité stable à la réinstallation de Suwayomi ; entier local en cache | EF-SRC-036, EF-SRC-008, ADR-0043 | M1a |
| `connected_service.instance_fingerprint?` ; capacité `INSTANCE_FINGERPRINT` | Réinstallation d'un service connecté détectée sans cascade | EF-SRC-036 | M1b |
| `session.auth_time`, `absolute_expiry`, `remember` ; `role_policy.fresh_auth_s` ; audit `session_id` + `auth_time` | Sessions bornées, fraîcheur des actions sensibles | EF-USR-022, ENF-SEC-010, ADR-0049 | M1a (session) / M1b (fraîcheur) |
| `device_token(subject_user_id, issued_by, …)` | Jetons d'appareil au profil lecteur | EF-USR-022, EF-USR-008 | M3 (crochet) / M5 |
| `edition.performance` (sentinelle `''`) dans la clé | Lectures audio d'un même texte | EF-BIB-001 | M1a (colonne) |
| `known_bad_versions[].category` += `auth` ; règle « compte restreint présent → toutes les étagères en pause » | Faille `access_control` de Kavita et étagères adultes | EF-EXP-055, ENF-COMP-001 | M1a |
| Assets de release `schema.sql` + `data-dictionary.md` (générés), copiés dans le jeu de sauvegarde | Base lisible avec `sqlite3` seul | EF-INT-027 | M1a |
| `store/INDEX.jsonl` (régénérable, fenêtre de maintenance) | `/store` lisible sans `/data` | EF-INT-027 | M1b |
| Sentinelle : `predecessor_instance_ids[]` (passe 11 : les pierres tombales des prédécesseurs se déchiffrent par les `kid` de leur trousseau importé, plus par un sel) ; `waiting_state` `foreign_root`, `root_lost`, `root_behind`, `store_lost`, `source_instance_changed` | Sorties d'une racine durablement différente | EF-EXP-064, EF-INT-023 | M1b |
| Miroir de provenance : `follow_suppressed`, `follow_from`, âge classé (relu en suggestion) | Reprise sans sauvegarde | EF-EXP-063 | M1a (format 1.2, champs facultatifs) |
| `content.derived_hash_by`, `content.derived_chain[]` ; original `untrusted` dans `/store` en mode passage | Mise à jour de sécurité du neutraliseur | EF-BIB-051, ADR-0047 | M2 |
| `FsOp.op = unlink` (hôte seul), `TargetCapabilities.can_purge`, `readback`, `render()`, `write_inline` ; espaces de noms `plugin.<clé>.*` ; attestation admin de `content_trust`/`access_partitioning` d'un plugin tiers | Compléments du contrat de destination | EF-EXP-065, ADR-0024, ADR-0011 | M3 (forme) / M4 (kit) / M6 (attestation) |

### 3.12 Ajouts de la passe 11 au modèle (L1–L6)

Même règle qu'aux passes 6 à 10 : tout ce qui est un **format sur disque**, une **identité** ou un **contrat** est posé en M1a ; le reste est daté par son jalon. Tests écrits avant le code (`03-roadmap-tdd.md`, blocs « Passe 11 »).

| Table / champ / format | Contenu | Exigence | Jalon |
|---|---|---|---|
| `content_tombstone` : `id`, `scope`, `seq`, `kid`, `alg`, `nonce`, `ct`, `created_at`, `epoch`, `reason` (opaque) ; plus aucune empreinte, plus de `salt_id` | Pierre tombale **chiffrée** : le clair (jamais stocké) contient les valeurs brutes de la portée — `variant` : `source_key`, sous-source, `remote_unit_id` tel que reçu, `content_hash`/`derived_hash` avec `content_hash_by` ; `place` : clé d'Édition brute (colonnes à sentinelle comprises), `track`, `number_segment`, `number_raw`, `part` ; `series_binding` : `source_key`, sous-source, `series_ref` brute ; `work` : `external_id` (`id_scheme`, valeur) — et les `normalizer_id@version` en vigueur à la purge | EF-BIB-054, EF-BIB-053, ADR-0048 | M1a (`work` : M2) |
| Journal `<racine>/.shelfhold/tombstones` **format 2.0** (JSON Lines, ajout seul) : `{v: "2.0", seq, scope, kid, alg: "xchacha20poly1305", nonce, ct}` | Même enveloppe que les secrets (ENF-SEC-005) ; **données associées** = `v` ‖ `scope` ‖ racine (`root_id` de la sentinelle depuis la passe 12, M2 — `mount_id` avant ; `db` pour la table) ‖ `seq` : une ligne recopiée sur une autre racine, réordonnée ou altérée ne se déchiffre pas ; l'AEAD remplace le HMAC 1.1 (intégrité comprise) | EF-BIB-054, ADR-0039, ADR-0048 | M1a |
| Clé `K_tomb` = HKDF-SHA256(clé du trousseau, contexte `shelfhold/tombstone/v1`) par `kid` | Distincte des clés de secrets ; suit la rotation **sans réécrire le journal** (les anciennes lignes gardent leur `kid`, passe 12, M1) ; un `kid` cité par une pierre tombale n'est **jamais retiré** du trousseau ; clés retirées gardées pour déchiffrer ; trousseau perdu ⇒ lignes indéchiffrables ⇒ journal **inerte** (ne bloque ni ne supprime, `tombstone.unverified`), risque accepté (Q64, R62) ; `shelfhold status` avertit tant qu'aucun `secrets export` n'a suivi la première pierre tombale | EF-BIB-054, ENF-SEC-005, EF-USR-023 | M1a |
| Index d'appartenance **en mémoire** : `{(portée, normaliseur@version) → formes normalisées}` | Construit au démarrage, après chaque rejeu et chaque purge ; jamais persisté en clair ni journalisé ; un candidat (référence, place, série, identifiant externe, fichier) est re-normalisé sous le normaliseur courant **et** chaque version historique citée ; un fichier est haché avec chaque `content_hash_by` cité | EF-BIB-054, ENF-OPS-021 | M1a |
| Registre des normaliseurs : `cited_by_tombstone` (calculé) | Une version citée n'est jamais retirée du code (contrôle CI `test_cited_normalizer_version_never_removed`) ; `normalize migrate` reconstruit l'index | ENF-OPS-021, ADR-0043 | M1a |
| Crochet `tombstone_check(candidate, alias_inverse?)` appelé par `normalize migrate` (M1a), la fusion d'Éditions (M2), `migrate_ref` et alias de source (M3), `replaced_by` (M6) | Une ligne vivante qui, migrée, correspondrait à une pierre tombale est bloquée (`tombstone.migration_hit`) ; l'alias inverse (forme ancienne ↔ forme nouvelle) est passé au test | EF-BIB-054, EF-BIB-049, EF-SRC-022, EF-SRC-030 | M1a (crochet) / M2 / M3 / M6 |
| Sentinelle et `instance` : `tombstone_count`, `tombstone_last_tag` | Détection de troncature du journal (`tombstone.journal_truncated`) ; complément depuis la table ou une autre racine | EF-BIB-054 | M1b |
| `role_policy` : axe `credential` ∈ {`fresh_session`, `session`, `api_key:<portée>`, `device_token`, `local_cli`} ; problème `…/problems/credential-not-allowed` ; `reauth-required` gagne `reauth_method` | Actions réservées refusées à toute clé et à tout jeton (I47) | EF-USR-024, ENF-SEC-010, ADR-0049 | M1a (axe, refus) / M1b (table exhaustive) |
| `api_key.expires_at` obligatoire pour `admin` ; `api_key.subject_user_id`, `issued_by` | Clé `admin` bornée si le profil durci l'exige (P4) ; clé `read`/`request` au nom d'un sujet | EF-USR-003, EF-USR-024 | M1a / M3 (sujet) |
| `work.age_rating` entier minimal (`0`, `10`, `12`, `13`, `16`, `18`), `work.age_x`, `work.rated_at` ; mêmes champs sur `edition` et `series` ; type SDK `AgeRating = {min_age: int, x: bool}` | Échelle ordinale : un palier s'ajoute sans migration ; libellés traduits ; règle d'I48 | EF-EXP-011, EF-BIB-037, EF-BIB-055 | M1a (colonnes, type) / M2 (règle d'I48) |
| `job_view.priority_class`, `job_view.request_id?` | Trois classes Huey fixes, rattrapage en lots | EF-INT-017 | M1b |
| Manifeste de plugin : `waiting_states[].exit_action` (`retry` \| `recheck` \| `reconfigure`) | Sortie générique des états propres `plugin.<clé>.*` | EF-EXP-065, EF-INT-023 | M3 |

### 3.13 Ajouts de la passe 12 au modèle (M1–M10)

Passe sans manque significatif : aucun nouveau format sur disque, aucune nouvelle identité ni rupture de contrat ; les ajouts ci-dessous sont des colonnes, une table d'attente et des capacités facultatives, posés au jalon où la règle s'applique. Tests écrits avant le code (`03-roadmap-tdd.md`, blocs « Passe 12 »).

| Table / champ / format | Contenu | Exigence | Jalon |
|---|---|---|---|
| Sentinelle `.shelfhold-mount-id` : `root_id` (ULID) | Identité **de la racine**, distincte de l'`instance_id` ; conservée par `root takeover` et `rebaseline` ; données associées des pierres tombales de la racine (GAP-12-02) | EF-BIB-054, EF-EXP-064, ADR-0022 | M1a (champ) / M1b (`takeover`) |
| `tombstone_pending_root` : `root_id`, `seq`, ligne chiffrée, `created_at` | Ligne de journal destinée à une racine `rw` absente pendant la purge ; écrite au remontage avant toute autre écriture sur la racine (GAP-12-14) | EF-BIB-052 | M1a |
| Trousseau : `kid.cited_by_tombstone` (calculé) | Un `kid` cité n'est jamais retiré (`secrets rotate --retire` refusé, contrôle au démarrage) ; `shelfhold status` avertit après chaque rotation sans nouvel export (GAP-12-01) | ENF-SEC-005, EF-BIB-054 | M1a |
| Événement `tombstone.key_unknown` ; commande réservée `shelfhold tombstone accept-lost-keyring` | Ligne au `kid` inconnu : bloque réadoption et réacquisition de sa portée sur sa racine, alerte « réimporter le trousseau » ; la dérogation tapée et auditée rend ces lignes inertes (R62) | EF-BIB-053, EF-BIB-054 | M1a |
| Socket `/data/shelfhold.sock` (`0600`, `SO_PEERCRED`) ; `role_policy.credential = local_cli` attribué par ce seul canal | CLI locale (GAP-12-03) ; hors serveur, la CLI prend le `flock` | EF-USR-024, ENF-OPS-015, ENF-SEC-010 | M1a |
| `role_policy.reserved` calculé par propriété (émetteurs d'`unlink`, vidage de corbeille, effacement de `.shelfhold/`, secrets, contrôle parental, confirmation du rejeu) ; `fresh_auth_s = 300` pour ces actions | Actions réservées par propriété (GAP-12-04) et fraîcheur minimale (GAP-12-05) | EF-USR-024, EF-USR-022 | M1a |
| `edition.rated_at` (avec `work.rated_at`) | Édition classée ⇔ `edition.rated_at` non nul ; règle d'I48 appliquée à `relink`, à l'appariement et aux étagères famille (GAP-12-09) | EF-BIB-055 | M1b |
| `edition.shelf_state = unshelved` ; vue « À ranger » | Édition sans étagère : non exportée ; source native ou import : copie dans `/store` au format reçu ; service connecté : rien n'est récupéré (GAP-12-11) | EF-EXP-046 | M1b |
| `reading_position.carrier_type`, `carrier_id` | Ancre polymorphe `export_item` \| `content` (AA5, ADR-0045) | EF-LEC-014 | M1a (colonnes) / M5 (lecteur) |
| Jeton `since` opaque = séquence de `event` ; retraits (`deleted`, `purged`, `merged`, `visibility_lost`) ; problème `…/problems/resync-required` | Sémantique de `?since=` (AA4) | EF-INT-001 | M3 (avant le gel 1.0) |
| Capacité de destination facultative `update_metadata` (champs, verrou) | Métadonnées sans réécriture pour une destination tierce ; repli : proposition de réécriture (GAP-12-13) | EF-EXP-001, EF-EXP-040 | M4 |
| `shelf_destination.kavita_series_age_locked` (vérifié après scan) | Âge de série poussé verrouillé pour les séries sans âge dans les fichiers (EPUB) (GAP-12-06) | EF-EXP-040, EF-EXP-033 | M2 |
| `docs/kavita-routes.md` ; fixtures `openapi.json` figées par version | Liste blanche des routes Kavita (route, méthode, usage, jalon), testée hors ligne contre chaque version de la plage (AA2) | EF-EXP-009, ENF-COMP-001 | M1a |

### 3.14 Ajouts de la passe 13 au modèle (N1–N6)

Deuxième passe sans manque significatif ; la boucle de revue est close. Aucun format cassé : une table de registre, une table de racines, deux champs de sentinelle, un registre de champs Kavita et un corps facultatif d'`ApiCall`, posés au jalon où la règle s'applique. Tests écrits avant le code (`03-roadmap-tdd.md`, blocs « Passe 13 »).

| Table / champ / format | Contenu | Exigence | Jalon |
|---|---|---|---|
| `tombstone_kid(kid, first_seq, created_at)` en base ; champ `tombstone_kids` de la sentinelle (ajout seul) | **Registre des `kid` émis** pour les pierres tombales : `kid` absent du trousseau et présent au registre ⇒ blocage (`tombstone.key_unknown`, `waiting_state` `tombstone_key_unknown`) ; absent du registre ⇒ ligne forgée, inerte (`tombstone.unverified`, cause `kid_unregistered`) ; seule source du contrôle « jamais retiré », racines démontées comprises (GAP-13-01, GAP-13-10) | EF-BIB-053, ENF-SEC-005 | M1a |
| `physical_root(root_id, entrée PathMap, st_dev vu, génération, prédécesseurs)` | `root_id` aussi en base ; repris par `root takeover` et par la recréation de la sentinelle ; rejeu sous les `root_id` connus, `tombstone.root_mismatch` (GAP-13-04) | EF-BIB-054, EF-EXP-064 | M1a (table) / M1b (`takeover`) |
| Sentinelle, bail `lease`, en-tête du journal de pierres tombales : `schema_version` `majeur.mineur` | Formats versionnés, champs inconnus conservés à la réécriture (GAP-13-05) | ENF-OPS-013, ENF-OPS-008 | M1a |
| `kid.state = compromised` ; intention `tombstone_rekey` | `shelfhold tombstone rekey` après une fuite : journal de chaque racine réécrit sous un nouveau `kid` (nouveau fichier, `rename` atomique ; racine démontée au remontage), table et `tombstone_pending_root` réécrites (GAP-13-06) | EF-BIB-054, ENF-SEC-005 | M1b |
| `role_policy.system_emitters` (liste fermée) | Émetteurs système exemptés de la règle des actions réservées, limités aux chemins créés par l'hôte ; sorties d'EF-INT-023 classées réservées ou non (GAP-13-02) | EF-USR-024, EF-INT-023 | M1a |
| Socket `/data/shelfhold.sock` : `SO_PEERCRED.pid = 0` refusé ; `SHELFHOLD_CLI_ALLOW_HOST` | Pair hors de l'espace de noms PID du conteneur refusé sauf réglage explicite, journalisé (GAP-13-07) | EF-USR-024 | M1a / M6 (plugins) |
| `waiting_state` `tombstone_key_unknown`, `route_missing` ; `shelf_destination.mode ∈ {normal, degraded_readonly, degraded_feature}` ; événements `target.route_missing`, `tombstone.root_mismatch` | Matrice de pannes et surface d'exploitation alignées sur la passe 12 (GAP-13-11) | EF-INT-006, EF-INT-023, EF-EXP-009 | M1a |
| `download_request.state = closed_by_purge` ; job unique de migration M1b | Tickets d'une Unité purgée fermés côté Shelfhold en M1a, `cancel(ticket)` en M1b ; reprise des copies amont laissées par les purges de M1a (GAP-13-09) | EF-BIB-052 | M1a / M1b |
| `field_owner(shelf_destination_id, export_series_id, field, last_value, pushed_at)` | Champs Kavita verrouillés **par Shelfhold** ; seuls réécrits ; écart ⇒ `reconcile.drift` ; invalidé et repoussé après réinstallation de Kavita (GAP-13-03) | EF-EXP-057, EF-EXP-040, EF-EXP-039 | M1a (table) / M1b (usage) |
| Retraits de `?since=` : filtre « déjà visible par ce lecteur » | Visibilité rejouée depuis les événements de classement et d'étagère (GAP-13-08) | EF-INT-001 | M3 |
| `ApiCall.body: JsonBody \| ContentBody(content_ref, encoding = raw \| multipart(fields, file_field))` | Corps facultatif soumis aux règles de `FsOp.write` ; `push` réservé dans le kit jusqu'à M4 (AB1) | EF-EXP-001 | M4 |
| Règle anti-écho à la réception | Empreinte d'un export existant ⇒ `replica`, jamais nouvelle Variante (I50, AB4) | EF-SRC-006 | M2 |

## 4. Contrats de plugins (`shelfhold.sdk`, ADR-0024)

En V1, le SDK est **interne au monorepo** (`backend/src/shelfhold/sdk/`). Il sera publié sur PyPI (`shelfhold-sdk`, MIT) en M6. La V1 implémente **Source** et **ExportTarget**. **MetadataProvider** arrive en M2. **Tracker, Importer, Notifier, Processor** sont des noms réservés dont le contrat est esquissé ci-dessous. Découverte par entry points `shelfhold.plugins`.

**Compatibilité** (EF-SRC-001) : l'hôte accepte `api == N` et `api == N-1` ; un **shim** traduit N-1 vers N. Il refuse les autres valeurs avec un message. L'état persisté d'un plugin porte `memo_version` (sur `SourceItem.memo`) et `payload_schema` (sur `plugin_state`), avec une fonction `migrate_memo(old_version, memo) -> memo`. `rpc_protocol` (M6) est versionné séparément de `api`. **Sérialisabilité** (passe 7, V4) : tout type du SDK fait un aller-retour JSON (`test_sdk_models_json_roundtrip`, M1a) ; aucune signature ne prend ni ne rend d'objet vivant hors `Fetcher` ; `bytes` en base64, `Decimal` en chaîne, `CookieJarView` en instantané, flux et dossiers des contrats réservés en chemins d'un répertoire partagé.

```python
# ---------- communs ----------
class PluginKind(StrEnum):
    SOURCE = "source"; EXPORT_TARGET = "export-target"; METADATA = "metadata"
    # réservés (contrat esquissé, non implémenté en V1)
    TRACKER = "tracker"; IMPORTER = "importer"; NOTIFIER = "notifier"; PROCESSOR = "processor"

class DomainRule(BaseModel):
    pattern: str                       # "example.org", "*.cdn.example.org" (suffixe/joker)
    purpose: Literal["api", "pages", "covers", "auth", "other"]
    base_url_setting: str | None = None   # ce domaine est surchargeable par un réglage ; surcharge journalisée (EF-SRC-003)

class Permissions(BaseModel):
    domains: list[DomainRule]
    lan_targets: list[str] = []        # "host:port" explicites (jamais un booléen) — ENF-SEC-001
    needs_browser: bool = False        # réservé (M2+), toujours derrière la même garde SSRF
    needs_solver: bool = False         # réservé (M2+)

class PluginManifest(BaseModel):
    key: str                           # stable, jamais dérivé du nom affiché ("gutenberg", "suwayomi")
    kind: PluginKind
    version: str                       # semver du plugin
    api: int                           # majeur du contrat ; N ou N-1 (shim)
    name: str
    permissions: Permissions
    settings_schema: dict              # JSON Schema → UI générée ; champs `secret` chiffrés
    content_warning: Literal["safe", "mixed", "nsfw"] = "safe"
    terms_url: str | None = None       # conditions d'utilisation (ENF-LEG-003)
    respects_robots: bool = True
    attribution: str | None = None     # texte d'attribution affiché dans l'UI (ENF-LEG-005)
    homepage: str | None = None

# ---------- âge (passe 11, L5, EF-EXP-011) ----------
class AgeRating(BaseModel):          # âge minimal entier + drapeau X ; libellés jamais dans le contrat
    min_age: int                       # 0, 10, 12, 13, 16, 18 (un palier s'ajoute sans casser le contrat)
    x: bool = False

# ---------- langues (passe 6, ENF-I18N-005, ADR-0042) ----------
LanguageTag = Annotated[str, AfterValidator(normalize_lang)]  # BCP 47 canonique (RFC 5646, alias CLDR) ; validé par l'HÔTE,
                                                              # le plugin transmet la valeur reçue, l'hôte garde lang_raw

# ---------- pagination, filtres ----------
class Cursor(BaseModel):
    token: str                         # opaque pour l'hôte ; jamais un numéro de page imposé
class Page(BaseModel, Generic[T]):
    items: list[T]
    next: Cursor | None

class FilterSpec(BaseModel):           # types de filtres en données (Select, Text, CheckBox, TriState, Group, Sort)
    key: str; kind: Literal["select", "text", "checkbox", "tristate", "group", "sort"]
    label: str; options: list[tuple[str, str]] = []; default: Any = None; children: list["FilterSpec"] = []

# ---------- authentification (EF-SRC-023) ----------
class AuthBasic(BaseModel):    kind: Literal["basic"] = "basic"
class AuthForm(BaseModel):     kind: Literal["form"] = "form"; fields: list[str]
class AuthApiKey(BaseModel):   kind: Literal["api_key"] = "api_key"; location: Literal["header", "query", "path"]; name: str
class AuthCookieImport(BaseModel): kind: Literal["cookie_import"] = "cookie_import"; domains: list[str]
class AuthDeviceCode(BaseModel):   kind: Literal["oauth2_device"] = "oauth2_device"; device_url: str; token_url: str; scopes: list[str]
class AuthInteractive(BaseModel):  kind: Literal["interactive"] = "interactive"; reason: Literal["captcha", "consent"]
AuthFlow = AuthBasic | AuthForm | AuthApiKey | AuthCookieImport | AuthDeviceCode | AuthInteractive
# V1 : Basic + ApiKey implémentés ; les autres flux sont réservés.

class SessionState(BaseModel):
    expires_at: datetime | None
    opaque: dict = {}                  # stocké chiffré par l'hôte, par utilisateur (source_account)
# Les secrets sont injectés par le Fetcher (en-tête, query, chemin) : le plugin ne les lit jamais en clair.
```

### 4.1 Source (V1)

```python
class Capability(Flag):
    SEARCH; LATEST; POPULAR; URL_RESOLVE; LOGIN; FILTERS; METADATA_ONLY; FOLLOW; SUB_SOURCES
    RANGED_CONTENT                     # passe 6 (EF-SRC-032) : la source livre un fichier couvrant une plage d'Unités (lncrawl, M2)

class SubSource(BaseModel):            # sous-sources dynamiques d'une méta-source (EF-SRC-021)
    source_key: str                    # ex. "suwayomi:2499283573021220255"
    name: str; lang: LanguageTag | Literal["mul"]   # "mul" : sous-source multilingue, jamais recopiée dans une Édition
    content_warning: Literal["safe", "mixed", "nsfw"]
    limit_key: str                     # clé de limitation par source logique amont (en plus du domaine)

class SourceManifest(PluginManifest):
    langs: list[LanguageTag]
    content_kinds: set[Literal["images", "text", "file"]]
    url_patterns: list[str]
    capabilities: Capability
    rate_limit: RateLimit              # requêtes/s et concurrence par domaine
    auth: list[AuthFlow] = []
    id_scheme: str = "v1"              # change → l'hôte attend des alias (EF-SRC-022)
    rights_basis: Literal["us_public_domain", "worldwide_public_domain", "licence", "user_supplied"] | None = None
    rights_jurisdiction: str | None = None   # ex. "US" pour Gutenberg (EF-SRC-024, ADR-0032)
    attribution: str | None = None     # chaîne d'attribution recopiée dans dc:rights (EF-BIB-033)
    translation_nature: Literal["published", "community", "machine", "unknown"] = "unknown"   # passe 7 (EF-BIB-049) ; surchargeable par série ou par Unité
    content_trust: Literal["publisher", "untrusted"] = "untrusted"   # passe 9 (EF-BIB-051, ADR-0047) : confiance dans le CORPS des fichiers ; untrusted ⇒ dérivé neutralisé avant export
    # Passe 9 (EF-SRC-035) : remote_id, SeriesRef, FileAsset.href et RangeRef sont opaques, SANS SECRET et stables à la rotation des identifiants ;
    # le Fetcher refuse (ou réécrit selon le gabarit {secret} déclaré) toute référence qui contient un secret connu ou une partie userinfo.
    tests: list[SourceTestCase]        # cassette **synthétique** hors ligne + canari autorisé (ENF-LEG-002/003)

class SeriesRef(BaseModel):
    source_key: str; remote_id: str    # url relative ou id
    title: str; cover: "ImageRequest | None" = None
    memo: dict = {}; memo_version: int = 1

class UnitInfo(BaseModel):
    remote_unit_id: str                # identité stable dans la source (ADR-0021)
    source_order: int                  # ordre tel que la source le donne
    number: str | None                 # texte brut ("12.5", "Extra 2", "Vol.3 Ch.10")
    number_sort: Decimal | None        # clé de tri proposée ; l'hôte peut la recalculer
    part: int | None = None
    track: Literal["main", "annual", "special", "extra", "oneshot"] = "main"
    kind: Literal["chapter", "episode", "book"] = "chapter"
    title: str | None; volume: str | None
    volume_label: str | None = None    # passe 6 : « Vol.N » lu dans le nom de chapitre → unit_coverage (origin=source_name)
    volume_closed: bool | None = None  # passe 6 : la source annonce que le tome est clos (EF-SRC-032, EF-BIB-040)
    team: str | None                   # libellé d'Équipe → team(edition, label normalisé) + team_alias(source_binding, label brut) (EF-BIB-035, passe 7)
    translation_nature: Literal["published", "community", "machine", "unknown"] | None = None   # passe 7 : surcharge par Unité
    lang: LanguageTag | None; lang_raw: str | None = None   # None → langue de la sous-source ; « all »/« und » → language_unknown
    published_at: datetime | None      # date_upload = 0 → None (jamais 1970-01-01) ; date partielle : published_on + precision (EF-BIB-047)
    availability: Literal["free", "purchased", "expiring", "locked", "stubbed", "removed"] = "free"
    available_until: datetime | None = None
    revision_hint: str | None = None   # empreinte/date de révision ; alimente le garde-fou (EF-BIB-024)
    memo: dict = {}; memo_version: int = 1

class ImageRequest(BaseModel):
    url: str; headers: dict[str, str] = {}; referer: str | None = None
    method: Literal["GET", "POST"] = "GET"; body: bytes | None = None
    expires_at: datetime | None = None # URL signée qui expire → re-résolution

class PageRef(BaseModel):              # résolution PARESSEUSE (A8)
    index: int
    token: str                         # opaque ; résolu à la demande par Source.resolve_page
    scrambled: "ScrambleInfo | None" = None   # déclare un désembrouillage requis

class ScrambleInfo(BaseModel):
    processor: str                     # clé d'un Processor "descramble" (réservé)
    params: dict

UnitPayload = (
    ImagePages(pages: list[PageRef])                                # l'hôte résout, télécharge, réessaie, hache
  | TextContent(xhtml: str, assets: list[ImageRequest])             # l'hôte assainit (nh3) ; réservé jusqu'à une source texte native
  | FileAsset(request: ImageRequest, mime: str, filename: str)      # passthrough CBZ/EPUB/PDF (octets reçus conservés, EF-SRC-015)
)

class Source(Protocol):
    manifest: SourceManifest
    def filters(self) -> list[FilterSpec]
    async def sub_sources(self, f: "Fetcher") -> list[SubSource]                  # si SUB_SOURCES
    async def search(self, f, q: str, filters: dict, cursor: Cursor | None, *, sub: str | None = None) -> Page[SeriesRef]
    async def resolve_url(self, f, url: str) -> SeriesRef | UnitInfo | None
    async def get_series(self, f, ref: SeriesRef) -> SeriesInfo                   # + external_ids, age_rating (AgeRating suggéré), series_hint
    async def list_units(self, f, ref: SeriesRef) -> list[UnitInfo]
    async def fetch_unit(self, f, ref: SeriesRef, u: UnitInfo) -> UnitPayload
    async def fetch_range(self, f, target: "RangeRef") -> "RangedPayload"   # si RANGED_CONTENT ; cas synchrone de la cible RangeRef (passe 7, EF-SRC-034)
    async def resolve_page(self, f, u: UnitInfo, p: PageRef) -> ImageRequest      # appelé juste avant le téléchargement
    async def login(self, f, flow: AuthFlow, creds: dict) -> SessionState         # si LOGIN
    async def refresh(self, f, s: SessionState) -> SessionState
    def migrate_ref(self, old_source_key: str, old_remote_id: str) -> tuple[str, str] | None   # alias (EF-SRC-022)
    def migrate_memo(self, version: int, memo: dict) -> dict

# Exceptions typées → état de santé et action UI (catalogue d'erreurs, EF-SRC-014, EF-UX-003)
class SourceError(Exception): code: str
class AuthRequired(SourceError): ...
class RateLimited(SourceError): retry_after: float | None
class ContentRemoved(SourceError): ...
class SourceChanged(SourceError): ...            # parseur cassé
class TemporarilyUnavailable(SourceError): ...   # service amont endormi/en panne → pause, pas échec

class UnitRef(BaseModel):              # passe 7 (EF-SRC-034)
    series: SeriesRef; unit: UnitInfo

class RangeRef(BaseModel):             # passe 7 (EF-SRC-034) : une plage d'Unités d'une même Édition
    series: SeriesRef; first: UnitInfo; last: UnitInfo
    volume_label: str | None = None

class RangedPayload(BaseModel):        # passe 6 : un fichier pour une plage (EF-SRC-032)
    asset: FileAsset
    covers: list[str]                  # remote_unit_id couverts, dans l'ordre → unit_coverage(origin=source_name, extent=full)
    bounds: dict[str, tuple[str, str]] = {}   # passe 7 : remote_unit_id → (start_locator, end_locator) si connus (EF-LEC-014)
    volume_closed: bool                # tome clos : hash figé (I13) ; sinon rolling_tail (EF-BIB-040)
```

Adaptateurs (ADR-0007, ADR-0008) :
- **natif** : entry point, plugins first-party (M1a).
- **méta-source Suwayomi** (M1a ; depuis la passe 6, première implémentation du port `ConnectedService`, §4.1 bis) : une `SubSource` par source Suwayomi installée par l'utilisateur ; `source_key = f"suwayomi:{id}"` ; `limit_key` fourni par la méta-source. Téléchargement par défaut du **CBZ entier** via `GET /api/v1/chapter/{id}/download`, jamais page par page en V1 (EF-SRC-008), seulement après l'état « téléchargé » (EF-SRC-028). La bibliothèque Suwayomi fait foi pour la liste des séries suivies (EF-SRC-009). Capacités propres à la méta-source (passe 3) : `request_refresh(series[])` (mutation GraphQL de rafraîchissement, mode piloté, EF-SRC-025) ; `semantic_health()` (sources chargées, chapitres en bibliothèque non téléchargés, fraîcheur, réglages de mise à jour globale, version `aboutServer` dans la plage, EF-SRC-026, ENF-COMP-001) ; `enqueue(variants[])` limité à la Variante retenue (EF-BIB-035). La méta-source n'installe ni ne met à jour aucune extension. **Passe 6 (U7, F12)** : l'adaptateur n'envoie que du GraphQL en `application/json` (le CSRF ajouté par Suwayomi-Server #2513 ne vise que les POST de type formulaire ; un 401 « CSRF mismatch » est classé `failure_class = version`, jamais `auth`, pour ne pas redemander d'identifiants) et n'appelle que les routes REST conservées par #2512 (`chapter/{id}/download` en GET et HEAD, page de chapitre, vignettes), liste blanche vérifiée par `test_suwayomi_adapter_sends_json_only_and_allowlisted_routes`.
- **lncrawl** (M2) : adaptateur REST vers **l'instance de l'utilisateur**, EPUB en passthrough ; second implémenteur de `ConnectedService` (`server_queue`) et premier utilisateur de `RANGED_CONTENT` (un EPUB par tome ou par plage de chapitres → `export_item` couvrant N Unités). Shelfhold n'active ni ne liste aucun crawler (EF-SRC-010).
- **OPDS générique** (M2), **Gutenberg** via catalogues RDF/CSV officiels ou miroirs, jamais le site web (EF-SRC-007), **Wikisource / Standard Ebooks** (EF-SRC-011), **dossier d'import** (EF-SRC-012).
- **rpc_stdio** (M6) : sous-processus JSON-RPC sans réseau, Fetcher par RPC (EF-SRC-016). **YAML déclaratif** : plus tard (EF-SRC-018).

### 4.1 bis Port `ConnectedService` (V1, passe 6, F9, ADR-0041)

Un **service connecté** (Suwayomi, lncrawl, et demain un service de la liste de veille) est une `Source` qui pilote un serveur tiers à état. Ce qui lui est propre passe par des **capacités déclarées et facultatives** ; le cœur (`acquisition`, `library`, `export`) ne programme que contre elles et ne contient **aucun** symbole propre à un service (import-linter).

```python
class ConnectedCapability(Flag):
    CATEGORIES; SERVER_QUEUE; RELEASE_CLAIM; UPSTREAM_RESTORE; HEALTH_PROBE; AUTH_MODE; WAKE; INSTANCE_FINGERPRINT   # passe 10 (M1b)

class ConnectedService(Source, Protocol):
    connected: ConnectedCapability
    # CATEGORIES — suivi par catégorie, exclusion du téléchargement automatique (EF-SRC-009)
    async def followed_series(self, f) -> list[SeriesRef]
    async def set_follow(self, f, ref: SeriesRef, on: bool) -> None                 # follow/unfollow (EF-SRC-029)
    # SERVER_QUEUE — acquisition en deux temps, ticket persisté dans download_request (GAP-6-15)
    # passe 7 (EF-SRC-034) : cible polymorphe ; un ticket par cible, Unités couvertes dans download_request_unit
    async def request(self, f, target: UnitRef | RangeRef) -> "Ticket"
    async def poll(self, f, t: "Ticket") -> Literal["queued", "downloading", "ready", "failed", "gone"]
    async def fetch(self, f, t: "Ticket") -> UnitPayload | RangedPayload
    async def cancel(self, f, t: "Ticket") -> None
    # RELEASE_CLAIM — purge ou retrait de la revendication (ui_login), sous I11 (EF-BIB-038, EF-SRC-031)
    async def release(self, f, t: "Ticket") -> "ReleaseResult"                      # espace libéré mesuré ensuite
    # UPSTREAM_RESTORE — relevé et restauration des réglages amont (EF-EXP-056) ; le relevé va dans upstream_snapshot,
    # table dédiée sauvegardée et jamais purgée sans détachement, jamais dans le journal d'intentions (passe 7, G7)
    async def snapshot_upstream(self, f, ref: SeriesRef) -> dict
    async def restore_upstream(self, f, ref: SeriesRef, snap: dict) -> None
    # HEALTH_PROBE — sonde fonctionnelle, version, réglages globaux (EF-SRC-026, ENF-COMP-001)
    async def semantic_health(self, f) -> "ServiceHealth"
    async def request_refresh(self, f, refs: list[SeriesRef]) -> None                # mode piloté (EF-SRC-025)
    # AUTH_MODE — mode d'authentification lu à l'ajout et à chaque sonde (EF-SRC-031)
    async def auth_mode(self, f) -> str
    # WAKE — délégué au port WakeHook de l'hôte ; le service ne réveille rien lui-même (EF-INT-022)
    wake_target: str | None
    # INSTANCE_FINGERPRINT — passe 10 (EF-SRC-036, M1b) : empreinte de l'instance de l'outil connecté ; un changement met le service en pause
    # (source.instance_changed) sans cascade EF-SRC-027 ; `shelfhold source reconnect` ré-résout par identité amont stable
    async def instance_fingerprint(self, f) -> str
```
**Identité amont stable (passe 10, K3, I42).** `remote_unit_id` et `SeriesRef` sont des identités de la **source amont**, jamais de la base de l'outil connecté : pour Suwayomi, `suwayomi:<sourceId>` + `chapter.url` (et `manga.url` pour la série), comme Mihon ; l'entier local de la route `/api/v1/chapter/{id}` est un `fetch_handle` en cache, ré-résolu par `(sourceId, chapter.url)` après une réinstallation ou une restauration `.tachibk`. Règle du contrat (doc auteur, kit de conformité) : ces identités **doivent** survivre à une réinstallation de l'outil ; le kit joue une réinstallation simulée et exige zéro nouvelle Variante.

Règles : une capacité absente désactive la fonction correspondante avec un message, jamais un cas particulier dans le cœur ; `test_minimal_connected_service_passes_follow_cycle` joue le cycle de suivi complet avec un faux service sans `CATEGORIES` ni `RELEASE_CLAIM` ; les sources synchrones (Gutenberg, OPDS) gardent `fetch_unit`.

### 4.2 ExportTarget (V1) — les destinations décrivent, le cœur appelle

Le cœur détient la clé admin Kavita et toute clé de destination ; elles ne sont **jamais** transmises au plugin (EF-EXP-001, ENF-SEC-013). Les opérations de la destination sont soit pures (rendu, contraintes), soit des **descriptions d'appels** (`ApiCall`) que le cœur exécute avec ses secrets, via son Fetcher (garde SSRF, `lan_targets`).

```python
class TargetCapabilities(BaseModel):
    media_kinds: set[MediaKind]; formats: set[FormatId]           # cbz, epub, pdf, kepub (M6)
    transport: Literal["tree", "dropbox", "push", "pull"]   # passe 6 (EF-EXP-058) : remplace `layout` ; Kavita/Komga "tree", CWA "dropbox",
                                                              # Send-to-Kindle/Calibre "push" (RÉSERVÉ dans le kit jusqu'à M4, passe 13), OPDS sortant/émulation Kobo "pull"
    egress: Literal["foyer", "externe"]                        # le fichier quitte-t-il le foyer ? watermarked → externe refusé (F3, ADR-0033) ; passe 7 : valeur PAR DÉFAUT, l'instance (shelf_destination) l'atteste
    can_update: bool; can_delete: bool                          # push sans retour (Kindle) : False/False
    can_purge: bool = False                                     # passe 10 (EF-EXP-065) : suppression définitive possible (≠ corbeille propre à la destination)
    readback: Literal["none", "verify", "fetch"] = "none"     # passe 10 : un exemplaire en destination sert-il de source d'octets (EF-BIB-045) ?
    confirms_arrival: bool                                      # Kavita True (polling/événements) ; push par e-mail False
    reconcilable: bool                                          # vérification du rangement, autre écrivain et sentinelle seulement si True (tree)
    accepted_content_types: list[str]                           # ordonnée, ex. Kobo [kepub, epub], Kindle [epub] (GAP-6-10) ; sélection en M4
    sidecars: set[Literal["comicinfo", "opf-embedded", "series.json", "cover-file", "metadata.json"]]
    path_scoped_scan: bool                                         # Kavita True, Komga False, ABS True (watcher)
    access_partitioning: Literal["none", "library", "user"]      # cloisonnement d'accès (EF-EXP-047) : Kavita/Komga "library", CWA/dossier "none"
    accepted_image_types: set[str]                                 # ex. Kavita {jpeg, png, webp, gif} pour les couvertures (EF-EXP-046)
    events: Literal["signalr", "sse", "none"]                      # confirmation d'arrivée (EF-EXP-034)
    collections: Literal["api", "metadata", "none"]
    progress: Literal["api", "kosync", "none"]
    manage_libraries: bool
    naming: NamingConstraints                                      # max_segment_bytes, max_path_bytes, forbidden, case_insensitive
    name_normalizer: str                                           # règle de normalisation des noms de série (EF-EXP-032)
    series_grouping: Literal["name", "folder"] = "name"            # passe 8 (GAP-8-09, M4) : Kavita par nom, Komga par dossier

class ApiCall(BaseModel):              # décrit, n'exécute pas
    method: str; path: str; query: dict = {}; json: Any = None
    body: JsonBody | ContentBody | None = None                     # passe 13 (AB1, M4) : ContentBody(content_ref, encoding = "raw" | Multipart(fields, file_field)) ;
                                                                   # mêmes règles que FsOp.write (export_item planifié pour cet appel et cette liaison, watermarked
                                                                   # refusé vers egress=externe, miroir jamais envoyé) ; hôte figé par la config, chemin relatif seulement
    auth: Literal["target_admin", "target_user", "none"]           # le cœur injecte le bon secret
    expect: list[int] = [200]

class FsOp(BaseModel):                 # passe 8 (H3, GAP-8-12) : plan d'opérations fichier SÉRIALISABLE, remplace l'objet vivant `f`
    op: Literal["mkdir", "write", "write_inline", "rename", "recycle", "touch_mtime", "unlink"]   # passe 10 : unlink émis par l'hôte seul (purge --now) ; write_inline borné
    rel_path: str                                                  # relatif à la racine de la liaison ; l'hôte refuse toute évasion
    dst_rel_path: str | None = None                                # pour rename
    content_ref: ContentRef | None = None                          # pour write : référence (export_item, sidecar rendu), jamais d'octets bruts
    mtime: datetime | None = None

class ExportTarget(Protocol):
    capabilities: TargetCapabilities
    def config_schema(self) -> dict
    def default_template(self, kind: MediaKind) -> str
    def render_sidecars(self, item: ExportItem, cfg: dict) -> list[SidecarFile]   # pur, octets déterministes (ComicInfo v2.0 + champs du brouillon 2.1 : AgeRating, SortName, LocalizedSeries)
    def parse_name_call(self, name: str, lib_type: str) -> ApiCall | None      # Kavita : GET /api/plugin/parse
    def inventory_calls(self, remote_library_id: str) -> list[ApiCall]         # séries/unités existantes (adoption, collisions)
    def ensure_library_call(self, spec: LibrarySpec, current: dict | None) -> ApiCall   # GET puis PUT fidèle
    def scan_calls(self, remote_paths: list[str]) -> list[ApiCall]             # Kavita : POST /api/library/scan-folder
    def resolve_remote_calls(self, item: ExportItem) -> list[ApiCall]
    def interpret(self, call: ApiCall, response: dict) -> Any                  # parse les réponses pour le cœur
    def supported_versions(self) -> VersionRange                               # hors plage → degraded_readonly « aperçu seulement » ; 404 sur une route → degraded_feature
    def event_subscription(self) -> EventSubscription | None                   # hub/flux, noms d'événements attendus
    def progress_calls(self, ref: RemoteRef, p: Progress | None) -> list[ApiCall]   # pull/push (M4)
# Règle de plan (cœur) : étagère max < 18+ ⇒ capabilities.access_partitioning != "none", sinon liaison refusée (EF-EXP-047).
# Les destinations ne touchent jamais le disque : le cœur exécute le plan (fsops).
# Passe 8 (H3) : une destination qui a besoin d'opérations fichier rend list[FsOp] (aller-retour JSON testé) ; l'hôte valide puis exécute ; forme fixée avant la doc auteur (M3).
# Passe 8 (GAP-8-10) : deux liaisons tree dont les racines se recouvrent sont refusées (target.tree_overlap) jusqu'à l'ADR « arbre et indexeurs » (avant M4, Q48).
# Passe 8 (H1) : le miroir de provenance (§5.7) est écrit par le cœur pour les liaisons tree egress=foyer ; jamais transmis à une destination egress=externe.
# Passe 9 (GAP-9-09, I37) : tout FsOp qui vise .shelfhold-mount-id ou .shelfhold/** (manifest, staging, recycle, tombstones, lease) est refusé par l'hôte.
# Passe 9 (J1) : le rendu part du dérivé neutralisé pour un EPUB untrusted ; file_hash est calculé sur ce rendu.
# Passe 10 (EF-EXP-065, I37) : un plan de destination qui contient unlink est refusé ; port render() pur et write_inline (taille bornée) pour series.json/metadata.json ;
#   événements, codes et waiting_state d'un plugin dans l'espace de noms plugin.<clé>.* ; access_partitioning et content_trust déclarés par un plugin TIERS
#   ne sont que des défauts proposés, attestés par l'admin par instance (comme egress) avant toute étagère famille ou export sans neutraliseur (M6).
# Passe 11 (GAP-11-10, M4) : un FsOp write « par référence » n'est accepté que si la référence est un export_item planifié pour cet appel et cette liaison.
# Passe 11 (GAP-11-09, M2) : publisher/content_trust déclarés par TOUT plugin non first-party (isolé ou non) exigent l'attestation de l'admin dès M2.
# Passe 11 (GAP-11-11, M3) : chaque waiting_state propre plugin.<clé>.* déclare une exit_action générique (retry | recheck | reconfigure), vérifiée par le kit.
# Passe 12 (GAP-12-13, M4) : capacité facultative update_metadata(fields, lock) déclarée avant le kit destinations ; Kavita first-party l'implémente
#   (âge de série verrouillé, EF-EXP-040) ; sans elle, un changement de métadonnées devient une proposition de réécriture (avertissement EF-EXP-037).
# Passe 12 (GAP-12-11) : une Édition unshelved (sans étagère) n'est jamais planifiée ; elle reste dans la vue « À ranger ».
# Passe 13 (AB1, AB2, AB3) : push exécutable par ApiCall.body ; Calibre par /cdb/* (add-book corps brut, set-fields sans verrou : lock ignoré et déclaré,
#   delete-books ; plage de versions étroite) ; Audiobookshelf jamais par POST /api/upload (arbre + scan ciblé) ; un e-mail (MailCall) s'ajoutera sans rupture.
# Clé du manifeste : (shelf_destination_id, export_item_id) + locator typé (path | remote_id | idempotency_key | aucun) (EF-EXP-058).
# Passe 7 (GAP-7-09) : liaison étagère en mode passage ↔ destination pull refusée sans copie conservée ni destination tree co-liée (source d'octets, EF-BIB-045).
# Passe 7 (G1) : le rendu (ComicInfo, OPF) est refait par destination à partir du canonique ; file_hash par (shelf_destination, export_item), content_hash commun.
# Opérations M4 : replace(old_items[], new_item) pour l'assemblage par volume ; la couverture est portée par unit_coverage (EF-BIB-044, EF-EXP-019).
# Choix du format : premier type de accepted_content_types disponible parmi les content de la Variante ; Rendition (M5) seulement sinon.
```

### 4.3 MetadataProvider (M2)

La forme reprend l'API `Source` des plugins de métadonnées de Calibre : capacités identify/cover, `touched_fields`, clé de pertinence, correspondance id↔URL. Seule la *forme* est reprise, **pas le code** (GPL-3.0).

```python
class MetadataProvider(Protocol):
    manifest: PluginManifest
    capabilities: set[Literal["identify", "cover", "release_calendar"]]
    touched_fields: set[str]                       # champs que ce fournisseur peut remplir (provenance)
    entity_types: set[Literal["series", "work", "edition", "volume"]]
    quota: ProviderQuota                           # seau à jetons + plafond journalier (ENF-LEG-005)
    async def identify(self, f, query: MatchQuery, *, timeout: float = 30) -> list[MetaMatch]
    def relevance_key(self, query: MatchQuery, m: MetaMatch) -> tuple
    async def get(self, f, ids: dict[str, str]) -> EntityMetadata  # champs + lang + provider
    async def get_cover(self, f, ids: dict[str, str]) -> ImageRequest | None
    def url_for(self, ids: dict[str, str]) -> str | None
    def ids_from_url(self, url: str) -> dict[str, str] | None

# Esquisse passe 6 (GAP-6-14), figée en M2 avec le premier fournisseur, élargie en M4 sans casser OpenLibrary :
#   async def identify(self, f, target: EntityRef, hints: MatchQuery) -> list[MetaMatch]   # entité visée explicite (un ISBN vise une Édition ou un Tome)
#   résultats : list[FieldProposal(entity_type, field, lang, value, confidence, provenance)] + list[RelationProposal]
#               (work_relation, official_links, licensed_in, calendrier de sorties pour series.stale, âge suggéré → age_suggestion)
#   auth: AuthFlow | None                    # réutilise EF-SRC-023 (OAuth par utilisateur, ex. Hardcover)
#   rate_limit déclaré dans le manifest (seau à jetons, provider_quota)
```
OpenLibrary en M2 (1 req/s anonyme, 3 req/s avec `User-Agent: Shelfhold/x.y (+contact)`). AniList (recherche de métadonnées **uniquement**, 30 req/min tant que l'API est dégradée), MangaUpdates (attribution obligatoire, cache ; `releases/days|search|rss` comme calendrier de sorties), Hardcover (côté serveur uniquement, OAuth ou jeton admin, jamais depuis le navigateur) et **BnF** (fournisseur intégré, SRU + SPARQL, Licence Ouverte, mention de la source et de la date de récupération) en M4 ; ComicVine en plugin optionnel désactivé par défaut ; Google Books avec clé de l'instance, budget 1 000 requêtes/jour réglable, suspension jusqu'à minuit heure du Pacifique sur 429 (EF-BIB-013, ENF-LEG-005). Aucun scraper de base BD propriétaire sans API dans le dépôt.

### 4.4 Contrats réservés (esquissés, non implémentés en V1)

```python
class Tracker(Protocol):        # M6 — par utilisateur, interdit par défaut aux profils enfant (EF-INT-010)
    async def link(self, f, account: SessionState, ids: dict) -> TrackerRef
    async def push_progress(self, f, ref: TrackerRef, p: TrackProgress) -> None
    async def pull(self, f, ref: TrackerRef) -> TrackProgress | None

class Importer(Protocol):       # M4 (.tachibk, EF-INT-008) ; plus tard Kavita/Calibre/CSV (EF-INT-013)
    def accepts(self, probe: ImportProbe) -> bool
    async def read(self, stream: BinaryIO, ctx: ImportContext) -> AsyncIterator[ImportRecord]
# ImportRecord : WorkHint, PendingSourceItem (résolution différée), ReadMarker, TrackerHint ; unit.state = known

class Notifier(Protocol):       # V1 : Apprise intégré au cœur ; contrat plugin réservé
    def accepts(self, event_type: str, schema_version: int) -> bool
    async def send(self, event: Event, cfg: dict) -> DeliveryResult

class Processor(Protocol):      # réservé ; premier usage prévu : "descramble"
    key: str; version: str
    input_kinds: set[str]; output_kind: str
    def params_hash(self, params: dict) -> str
    async def run(self, src: LocalFile, params: dict, work_dir: ScratchDir) -> ProcessorResult
class ProcessorResult(BaseModel):
    path: LocalFile
    original_sha256: str        # octets reçus (conservés)
    derived_sha256: str         # dérivé canonique déclaré
    lossless: bool              # PNG sans perte par défaut ; réencodage seulement si exigé (EF-BIB-006)
```

### 4.5 Contrat hôte `Fetcher` (V1 minimal, A13)

```python
class Fetcher(Protocol):
    async def get(self, url: str, *, headers=None, params=None, referer=None, cache_ttl: int | None = None) -> Response
    async def post(self, url: str, *, data=None, json=None, headers=None) -> Response
    async def download(self, req: ImageRequest) -> DownloadedFile     # Range + .part, sha256, plafonds de taille
    def cookies(self) -> CookieJarView                               # lecture seule pour le plugin
```
Garanties de l'hôte (ENF-SEC-001) :
- **Transport V1** : `httpx` (ou le fork `httpx2`) avec `httpx-secure`, `trust_env=False`. La garde SSRF s'applique après résolution DNS et à chaque redirection ; les redirections sont suivies manuellement et l'IP validée est épinglée.
- **Allowlist** `DomainRule` issue du manifest ; les cibles LAN ne sont joignables que par `lan_targets` explicites (`host:port`). **Passe 11 (Z1)** : un nom de `lan_targets` n'est autorisé que pour les adresses résolues à sa déclaration (IPv4, IPv6 ULA `fd00::/8` et lien-local compris), revalidées à chaque connexion (`test_lan_target_name_pinned_to_declared_addresses`).
- **Résolveur filtrant** (passe 11, Z1) : une réponse `0.0.0.0` ou `::` (bloqueur DNS du foyer) est classée `source.dns_blocked`, ni `ssrf.blocked` ni panne de source (EF-UX-003).
- **Limitation** : seau à jetons par domaine **et** par `limit_key` (source logique amont) ; `429`/`Retry-After` respectés.
- **Cookies et secrets** : cookies et UA persistés chiffrés par source et par utilisateur ; secrets AuthFlow injectés par le Fetcher.
- **Cache** court + ETag/Last-Modified ; journalisation avec `plugin_id`, secrets rédigés.
- **Références sans secret** (passe 9, EF-SRC-035, I38) : le Fetcher connaît les valeurs secrètes qu'il injecte ; toute référence rendue par un plugin qui en contient une, ou qui porte une partie `userinfo`, est refusée (erreur de contrat) ou réécrite selon le gabarit `{secret}` déclaré par l'adaptateur ; la rédaction des journaux couvre les segments de chemin et les paramètres de requête ; prérequis d'entrée de M2.
- **Réservé (M2+, première source native qui l'exige)** : `curl_cffi` avec motif « résoudre, valider, `CurlOpt.RESOLVE`, redirections manuelles », solveur, navigateur headless, proxy par source. Tous passent **derrière la même garde**.

---

## 5. Stockage

### 5.1 `/data` (volume privé, « chaud »)

Disposition fixée dès M0 (ENF-OPS-012, ADR-0028) : **deux volumes** distincts, pour qu'un NAS puisse mettre la base sur SSD et les fichiers sur disques durs sans migration ultérieure.

```
/data/                                                # SSD conseillé, local (NFS/CIFS refusé, ENF-OPS-010)
  shelfhold.db  shelfhold.db-wal  shelfhold.db-shm   # SQLite WAL
  huey.db                                             # file Huey (fichier séparé)
  config.toml                                         # schema_version (ENF-OPS-013)
  meta/units/{ulid[0:2]}/{unit_ulid}.json            # unit.json pour TOUTE Unité (mode passage compris) : métadonnées,
                                                      # provenance, pages [{index, hash, w, h}], hashes "sha256:<hex>"
  meta/works/{ulid[0:2]}/{work_ulid}.json             # work.json ; arborescence PLATE par ULID (passe 6, F8, ENF-OPS-020) :
                                                      # indépendante de la hiérarchie, une fusion ne déplace aucun fichier
  cache/http/  cache/thumbs/                          # régénérables
  plugins/                                            # wheels tierces (désactivées par défaut)
  keys/master.key (0400, si générée)                  # à exporter via l'assistant (Q25)
```
**Passe 6 (F8, GAP-6-08, ENF-OPS-020)** : **la base fait autorité**. `unit.json` et `work.json` sont des **projections régénérables** : écrits après la validation de la transaction, comparés et réparés au démarrage (une divergence est réécrite depuis la base et journalisée), reconstruits en entier par `shelfhold meta rebuild` ; une restauration d'un `VACUUM INTO` plus ancien que les JSON régénère donc les JSON. L'arborescence est plate et ne dépend que de l'ULID : fusion, séparation, `relink` ou changement d'Édition ne déplacent aucun fichier. `meta/` est inclus dans le jeu de sauvegarde (§5.4). Les hashes sont notés `sha256:<hex>` dans `unit.json`, les manifestes et BagIt (GAP-6-25). L'ancienne arborescence `meta/works/…/units/` (passe 5) est migrée par un job de format majeur (ADR-0039).

**Passe 5 (E7)** : `unit.json` vit **toujours** sous `/data/meta/…`, dans tous les modes (passage, copie conservée, adopté), jamais dans `/library` ni dans `/store` ; avant une migration majeure d'un format sur disque, les fichiers touchés sont copiés dans `/data/backups/formats/<révision>/` (§5.6).

Il n'y a ni CAS, ni refcount, ni GC de blobs en V1 (ADR-0010 amendée). Les sauvegardes partent vers une **destination distincte** (§5.4). Elles ne restent pas dans `/data`.

### 5.1 bis `/store` (volume « froid », copies conservées)

```
/store/                                               # disque dur possible ; même système de fichiers que /library si l'on veut le reflink ou le lien dur optionnel (contenu remplaçable)
  works/{work_ulid}/editions/{edition_ulid}/units/{number_sort_padded}-{unit_ulid}/
    content.cbz | content.epub | content.pdf          # tel que reçu (original_sha256) ; toujours l'original, jamais un rendu (passe 7, G1, I25) ;
                                                      # si la source a disparu : original extrait du fichier rendu, content_hash vérifié (store_provenance = extracted)
    content.derived.cbz                               # optionnel : dérivé canonique déclaré (derived_sha256)
  untrusted/{unit_ulid}/original.epub                 # passe 10 (K9, M2) : original d'un EPUB untrusted d'une étagère en MODE PASSAGE (seule exception au mode passage)
  INDEX.jsonl                                         # passe 10 (K7, M1b) : ULID → titre, langue, numéro, sha256 ; régénérable, réécrit dans la fenêtre, sans Unité sous pierre tombale
```
`/store` ne contient que les Unités des étagères en mode « copie conservée » (§5.5), les adoptions « intégrées au magasin » et, depuis la passe 10, les **originaux des EPUB `untrusted`** des étagères en mode passage (sans eux, une correction du neutraliseur ne pourrait repartir que du dérivé, §5.8). Il est **hors du jeu de sauvegarde** de Shelfhold : la doc admin le désigne comme à confier à l'outil de sauvegarde de l'hôte (ZFS/btrfs avec scrub, règle 3-2-1 conseillés). Les jobs qui le lisent (intégrité par roulement, vérification) ne tournent que dans la **fenêtre de maintenance** (ENF-OPS-014), pour laisser les disques se mettre en veille.

### 5.2 `/library` : montage du parent existant et correspondance de chemins (ADR-0022)

On monte **le parent existant** des médias Kavita (ex. `/srv/media/kavita`) sur `/library` dans Shelfhold. **On ne change rien au compose Kavita**. La table de correspondance par défaut traduit les chemins locaux en chemins vus par Kavita :

| Shelfhold voit | Kavita voit |
|---|---|
| `/library/manga` | `/manga` |
| `/library/books` | `/books` |
| `/library/comics` | `/comics` |

Réglage `SHELFHOLD_PATH_MAP` (ou par destination). « Même chemin des deux côtés » reste une **option**.

```
/library/                                  # = /srv/media/kavita sur l'hôte (exemple)
  .shelfhold-mount-id                      # sentinelle : instance + compteur de génération (EF-EXP-029)
  .shelfhold/lease                         # bail d'instance horodaté (ENF-OPS-015) ; un par racine physique (passe 7, GAP-7-13)
  .shelfhold/staging/<uuid>                # écriture atomique, même montage (EXDEV)
  .shelfhold/recycle/YYYY-MM-DD/           # corbeille, purge 30 j (ENF-OPS-009)
  .shelfhold/manifest/                     # miroir de provenance de la racine (passe 8, H1, EF-EXP-063, ADR-0046) : lignée par (chemin relatif, file_hash), format versionné
  manga/<Série>/…   books/<Série>/…   comics/<Série>/…   # arbres des bibliothèques Kavita (existants ou nouveaux)
  books/                                   # SI books/ est un autre disque monté (st_dev différent) : racine physique distincte
    .shelfhold-mount-id  .shelfhold/{staging,recycle,manifest}/   # sa propre sentinelle, son staging, sa corbeille (passe 6, EF-EXP-059), son miroir de provenance (passe 8)
/upstream/suwayomi/                        # optionnel, PathMap mode adopt_ro, montage :ro (passe 6, EF-EXP-060) : lu pour
                                           # l'adoption et le rapatriement, jamais écrit ; retiré après la bascule
```

**Racines physiques (passe 6, F5, EF-EXP-059).** Le parent monté sur `/library` contient souvent des sous-dossiers qui sont des **points de montage distincts** (un disque pour `manga`, un autre pour `books`). Au démarrage, chaque entrée de `PathMap` est sondée par `statfs` et `st_dev` : chaque racine physique distincte reçoit **sa** sentinelle, **son** `.shelfhold/staging`, **sa** corbeille `.shelfhold/recycle` et son contrôle de bail (le bail d'instance reste unique, à la racine de `/library`). On sonde aussi ses capacités : stabilité des inodes (NFS, CIFS sans `serverino`), reflink, `openat2`. Une racine absente (sous-disque non monté : le dossier vide qui reste ressemble à une bibliothèque vidée) → écritures, suppressions et scans **refusés vers elle**, `mount.sentinel_missing` avec la racine en cause ; les autres racines continuent. Une écriture qui changerait de racine passe par copie, vérification du hash et intention (`fs_intent.op = copy_cross_root`), **jamais** par `rename` (I24). La règle « reflink ou lien dur seulement sur le même système de fichiers » se juge par racine physique. `test_library_subdir_on_other_device_uses_local_staging_and_sentinel`.

**Identité de fichier (passe 6, GAP-6-13).** Un fichier géré est identifié par (ino, taille, mtime), puis par son hash en cas de doute ; `dev` ne sert qu'à juger « même racine ». Après un remontage NFS (`dev` change) ou sur une racine dont l'inode n'est pas stable, une intention ou la détection d'un autre écrivain se résout par taille, mtime et hash, sans faux « autre écrivain ».

**Racine `adopt_ro` (passe 6, F6, EF-EXP-060).** Cas central de la bascule : le volume de téléchargement de Suwayomi est monté directement dans Kavita, hors du parent partagé. Une entrée `PathMap` de mode `adopt_ro` (montage `:ro` dédié, par exemple `/upstream/suwayomi`) le rend lisible par `safe_open` sous racine (`openat2(RESOLVE_BENEATH)`) pour l'adoption (ComicInfo, hash, `plugin/parse`), le test `(dev, ino)` et le rapatriement (EF-EXP-051) ; elle n'est **jamais** une cible d'écriture (`fsops` refuse, contrat import-linter, `test_adopt_ro_root_never_written`). Les fichiers qu'elle contient ont des `replica.location = adopted_ro` et ne comptent pas comme copie indépendante sous Shelfhold. Le montage est retiré du compose une fois la bascule faite.

Règles :
- **EXDEV** : `link()` et `rename()` échouent entre deux montages. Staging et corbeille sont donc **dans** `/library`, et plus précisément dans **chaque racine physique** (passe 6, EF-EXP-059). En mode passage, le fichier construit en staging est **renommé** à sa place (aucune copie). En mode « copie conservée », l'export se fait **par copie indépendante** depuis `/store` (reflink si possible, EF-BIB-027) ; le **hardlink** est une option de M2 réservée au contenu **remplaçable**, possible seulement si `/store` et `/library` partagent le même système de fichiers **et** le même montage (EF-EXP-015), jamais pour une Unité irremplaçable ni une mise à l'abri (D9). Jamais de symlink.
- **Propriétaire** : pas de `chown`. Les fichiers appartiennent à l'uid/gid du conteneur (1000 par défaut), avec `SHELFHOLD_UMASK` configurable et `group_add` si nécessaire (EF-EXP-006). Au démarrage et à la création d'une liaison, une **sonde** vérifie l'écriture, le `rename` et le `mkdir` par l'uid, puis la lecture par Kavita. Elle écrit `.shelfhold-probe-<uuid>.epub`, puis appelle `plugin/parse` et un scan. En cas d'échec, le bloc YAML exact à corriger est proposé (EF-UX-002).
- **Sous racine** : toute écriture ou suppression passe par `openat2(RESOLVE_BENEATH\|RESOLVE_NO_SYMLINKS)` sur une racine `rw`, avec repli `O_NOFOLLOW` + vérification `dev/ino`, et par des `renameat` relatifs aux descripteurs. Les liens symboliques sont **refusés** dans les arbres gérés (ENF-SEC-012).
- **Sentinelle** (EF-EXP-029) : elle porte l'identifiant d'instance et un **compteur de génération** incrémenté à chaque session d'écriture ; une copie divergente du dossier (ancienne copie sur un autre hôte ou une autre VM) a une génération en retard et est refusée. Si `.shelfhold-mount-id` est absent, différent ou en retard, ou si les « manquants » dépassent 5 % ou 50 fichiers d'une liaison, la vérification du rangement, les suppressions et les scans sont **bloqués** et `mount.sentinel_missing` est émis. Hors fenêtre de maintenance, la sentinelle n'est lue qu'avant une écriture.
- `.shelfhold/` est hors de toute racine de bibliothèque Kavita et ajouté aux `ExcludePatterns` par sécurité (les réglages d'avant l'adoption sont mémorisés et restaurés au détachement, EF-EXP-031) ; un test nightly contre Kavita réel vérifie qu'un CBZ déposé dans `.shelfhold/` d'une racine n'est jamais indexé (passe 6, GAP-6-26 ; sinon le motif est proposé avec confirmation, EF-EXP-009).
- **Liens par chemin relatif** (EF-EXP-039) : toute liaison est clé par chemin relatif sous la racine de la liaison ; une ligne du manifeste est clé par `(shelf_destination_id, export_item_id)` et **résolue** par chemin relatif pour le transport `tree` (passe 6, EF-EXP-058) ; les identifiants Kavita ne sont qu'un cache (§6.6).
- `/data` sur NFS/CIFS est refusé (détection `statfs`, option pour passer outre, ENF-OPS-010). `/library` peut être réseau, mais la sentinelle reste obligatoire.

### 5.3 Contenu
- Les octets reçus ne sont **jamais réencodés**. Un dérivé canonique déclaré est permis : `original_sha256` + `derived_sha256`, PNG sans perte par défaut (EF-BIB-006).
- Pas de déduplication par page.
- XML : point d'entrée unique `safe_parse_xml()` (`defusedxml` ; ou lxml avec `resolve_entities=False`, `no_network=True`, `huge_tree=False`), plafonds de taille et de profondeur, pour OPF, ComicInfo, OPDS, RDF Gutenberg, SVG ; corpus XXE fichier, XXE http, billion laughs (ENF-SEC-014).
- Fichiers **tatoués** (`watermarked`, ADR-0033) : seul l'OPF peut être réécrit, par remplacement ciblé de son entrée dans l'archive, toutes les autres entrées recopiées octet pour octet (invariant I14) ; exclus de tout paquet de diagnostic.
- Archives : point d'entrée unique `safe_open_archive()`. Lecture en flux **sans extraction**, plafonds (entrées, taille, ratio), `tarfile` avec `filter='data'` si jamais utilisé (ENF-SEC-002). `MAX_IMAGE_PIXELS` et plafonds de pixels par `kind` (les webtoons hauts sont légitimes), décodage dans un worker borné (`RLIMIT_AS`) (ENF-SEC-003).
- Fichiers protégés par une mesure technique : à l'import, ils sont détectés (`META-INF/encryption.xml` hors obfuscation de polices, `rights.xml`) et **refusés avec un message**. Aucune tentative de levée (EF-SRC-012).

### 5.4 Sauvegarde, restauration, export ouvert (ADR-0023)
- **Jeu de sauvegarde** (EF-INT-015, ENF-OPS-001) : **`/data`** (snapshot `VACUUM INTO` + `config.toml` + `meta/`, régénérable mais sauvegardé pour une restauration sans reconstruction, ENF-OPS-020) + manifestes + clé maîtresse **chiffrée par phrase de passe**. **`/store` n'en fait pas partie** (§5.1 bis) ; le contenu se restaure par ré-adoption de la bibliothèque Kavita et re-téléchargement. Il part vers une **destination distincte** configurable : un autre disque/montage, ou une cible distante via Apprise/rsync documenté.
  - Rétention N jours.
  - Alertes `backup.failed`/`backup.stale`.
  - Objectifs : **RPO 24 h, RTO 1 h**, le RTO portant sur la **remise en service** (base, secrets, liaisons), pas sur le contenu.
  - Sauvegarde pré-migration nommée par révision, hors rotation.
  - Purge de la corbeille ≥ rétention des sauvegardes.
  - ≥ 2× la taille de la base libre avant `VACUUM INTO` (ENF-OPS-006).
- **Lisibilité à long terme** (passe 10, K7, EF-INT-027) : chaque release publie `schema.sql` et un dictionnaire de données générés, copiés dans le jeu de sauvegarde ; `store/INDEX.jsonl` (M1b) rend `/store` lisible sans `/data` ; export des suivis OPML et CSV (M3) ; `export-all` n'emporte aucun contenu sous pierre tombale (EF-BIB-053).
- **M1a** (passe 5, GAP-5-17) : pas de sauvegarde intégrée ; la doc fait sauvegarder `/data` **chaque jour** (RPO 24 h) par l'outil de l'hôte, Shelfhold arrêté (exemple : arrêt par l'orchestrateur à 07 h 15, après la fenêtre, copie, redémarrage) ; la clé maîtresse s'exporte par `shelfhold secrets export --passphrase-file … --out …` au format **age v1** (destinataire scrypt, armure ASCII), le même que celui de la clé incluse dans la sauvegarde intégrée de M1b.
- **Restauration** testée en CI sur un chemin différent ; `shelfhold reconcile --adopt-after-restore`. **Passe 7 (G4, ADR-0044)** : toute restauration pose `instance.restore_epoch` ; une base en retard sur une sentinelle démarre en `restored_behind` (lecture seule, rapport), les intentions de l'instantané passent `stale_from_backup` sans être rejouées, et seule `reconcile --adopt-after-restore` en sort (§6.7 bis). `upstream_snapshot` est dans le jeu de sauvegarde. Restauration **sans `/store`** : aucune suppression, Unités concernées à `store_missing`, ré-adoption puis re-téléchargement proposés.

### 5.5 Mode passage et copie conservée (ADR-0028)

Le mode est un réglage d'**Étagère** (`shelf.retention_mode`).

| Mode | Défaut pour | Ce que Shelfhold garde | Exemplaires du contenu sur l'hôte | Vérification « manquant » |
|---|---|---|---|---|
| **Passage** (`passthrough`) | Services connectés (Suwayomi, lncrawl, OPDS distant) | `unit.json`, hash reçus et exportés, manifeste (dans `/data`) | Dossier de téléchargement de Suwayomi + `/library` (×2) ; ×1 avec l'option « purger le téléchargement Suwayomi après export réussi » (désactivée par défaut, B6) | Re-téléchargement par la source si disponible, sinon question à l'admin (EF-EXP-018) |
| **Copie conservée** (`keep`) | Sources natives, dossier d'import, étagères marquées « irremplaçable » | En plus, le fichier reçu dans `/store` | `/store` + `/library` (×2 ; reflink si le système de fichiers le permet) ; ×1 avec le lien dur optionnel, réservé au contenu remplaçable (D9) | Réexport depuis `/store` |
| **Adopté** (`adopted_ref`) | Fichiers d'une bibliothèque Kavita adoptée | Référence au fichier de `/library` ; option « intégrer au magasin » → `keep` | `/library` seulement | Signalé seulement (EF-EXP-028) |

Règles :
- Le staging construit le fichier final (ComicInfo écrit en **dernière** entrée du ZIP, EF-EXP-040) puis le renomme ; aucun fichier intermédiaire ne reste.
- **Purge Suwayomi** : seulement après `unit.exported` **et** vérification du hash exporté ; jamais pour une Unité en quarantaine ; jamais si la règle « Mettre à l'abri » ne l'autorise pas (invariant I11).
- **Mettre à l'abri** (EF-BIB-038, C6) : avant toute purge et en mode passage, si la source est marquée fragile ou disparue, ou si l'Unité est candidate « irremplaçable » (dès **2 erreurs de contenu consécutives** de sa source amont ; une erreur d'authentification, de version, de schéma ou de réseau ne compte jamais, EF-BIB-042, D8), le job `library.safeguard` crée une **copie indépendante** du fichier de `/library` vers `/store` (reflink ou copie, **jamais un lien dur** : un lien dur partage l'inode, et une modification sur place toucherait les deux noms, D9) sous journal d'intentions, dans la limite du **quota** (taille estimée ≤ espace libre de `/store` moins la réserve, lots plafonnés, sinon `unit.safeguard_backlog`, EF-BIB-042), émet `unit.safeguarded` et met à jour la ligne Santé « irremplaçables sans copie conservée ». Échec (pas de `/store`, disque plein) → `unit.safeguard_failed`, purge refusée.
- **Irremplaçable** (EF-BIB-031) : `unit.irreplaceable` est recalculé à chaque changement de liaison ou de disponibilité. Une Unité irremplaçable en mode passage déclenche une suggestion « conserver une copie ». La rétention, le nettoyage et la suppression ne la retirent jamais sans confirmation tapée (invariant testé). PAR2 : jamais ; manifeste de fixité BagIt avec l'export intégral en M4.
- **Registre des exemplaires (passe 6, F4, EF-BIB-045)** : chaque exemplaire connu d'une Unité ou d'un `export_item` est une ligne `replica` (amont Suwayomi, `/library`, `/store`, destination, `adopted_ro`). Une **seule** fonction, `independent_copies(unit)`, compte les exemplaires indépendants (inode distinct, non lien dur, hash vérifié) ; la purge (I11), `irreplaceable`, « Mettre à l'abri », la page Santé et le fan-out M4 l'appellent toutes, sans requête ad hoc (contrat import-linter). **Source des octets** pour une mise à l'abri, un fan-out M4 ou le lecteur M5 : `/store` s'il existe, sinon le fichier de destination **seulement si son hash correspond au manifeste** (Kavita ou KOReader ont pu le modifier : `mutable_by_third_party`), sinon un nouveau téléchargement. Une Œuvre **non exportée** n'est lisible dans le lecteur intégré (M5) que si une copie conservée existe : en mode passage, elle n'a aucun octet local (correction de `01` §1.3).
- **Budget disque** documenté par mode (ENF-OPS-005).
- **Export intégral en format ouvert** (EF-INT-016, M3) : JSON versionné et documenté + arborescence de fichiers, à la manière du `document_exporter` de Paperless, avec réimport aller-retour. C'est la garantie de pérennité promise par la gouvernance (ENF-GOV-001).

### 5.6 Formats sur disque versionnés et migrations en avant (ADR-0039)

- **Formats concernés** : `unit.json`, `work.json`, manifestes lisibles, `config.toml`, sentinelle, fichiers de `.shelfhold/` (dont le **miroir de provenance** `.shelfhold/manifest/`, passe 8) ; chacun porte `schema_version = "majeur.mineur"`.
- **Mineur** : ajout de champs seulement ; N-1 lit le fichier et **conserve les champs inconnus** quand il le réécrit.
- **Majeur** : jamais à la lecture ; job explicite `formats.migrate` inscrit comme intention, lancé **après** la fenêtre de retour arrière de la version (au plus tôt à la version mineure suivante), précédé d'une copie des fichiers touchés dans `/data/backups/formats/<révision>/`.
- **Sens unique** : aucune migration descendante ; une version qui lit un format plus récent que le sien le traite en **lecture seule** (aucune réécriture, `reconcile.drift`) et refuse toute écriture qui le toucherait.
- **Base** : `shelfhold migrate` refuse de démarrer s'il reste des intentions ouvertes (ENF-OPS-019) ; le schéma de `fs_intent` est gelé et versionné.
- **Retour arrière** : tag précédent + sauvegarde pré-migration de la base + fichiers sauvegardés du format ; E2E « retour arrière après migration des formats » (ENF-OPS-008).

### 5.7 Miroir de provenance par racine physique (passe 8, H1, EF-EXP-063, ADR-0046)

- **Pourquoi** : un fichier rangé est figé (I22, EF-EXP-040) et ne porte, par choix de discrétion, aucune identité Shelfhold (ADR-0040, ADR-0030) ; sans copie de la lignée **à côté** des fichiers, une perte de `/data`, une restauration en retard ou une réinstallation réadopteraient tout en `adopted` (source, identifiant distant et équipe perdus).
- **Où** : `<racine physique>/.shelfhold/manifest/`, un miroir par racine (EF-EXP-059), dossier caché exclu de Kavita (`ExcludePatterns`, test nightly « jamais indexé ») ; il voyage avec les fichiers (copie de disque, rsync), pas avec `/data`.
- **Forme** : fichiers JSON Lines par liaison et par série exportée (`<shelf_destination_ulid>/<export_series_ulid>.jsonl`), `schema_version = "majeur.mineur"` (ADR-0039) ; une ligne par `export_item` : `rel_path`, `file_hash`, `content_hash`, `export_item` (ULID, `frozen_attrs`), Unités couvertes et bornes, Variante (`source_key`, sous-source, `remote_unit_id`), Équipe (ULID, libellé), `template_id@version`, Édition, série exportée, `written_at`. Aucune URL, aucun nom de domaine de source, aucun secret.
- **Écriture** : par le cœur, sous intention (EF-EXP-041), dans la même session d'écriture que le fichier, **après** lui, par écriture atomique (staging + rename) ; une ligne sans fichier est ignorée, un fichier sans ligne retombe sur l'appariement par hash et chemin. La base fait autorité : `shelfhold meta rebuild` régénère le miroir.
- **Lecture** : réconciliation, `reconcile --adopt-after-restore` (§6.7 bis), adoption et réadoption après réinstallation : `file_hash` concordant → état `exported` avec sa lignée ; `--rebaseline-inodes` (M1b) s'en sert pour confirmer un déplacement de disques.
- **Jamais** : dans un fichier exporté (ni annexe d'archive, ni `<Notes>`, ni `shelfhold:id`), vers une destination `egress = externe`, dans un paquet de diagnostic (I33). ComicInfo `Translator` = libellé d'Équipe (métadonnée publique) ; `Web` vide par défaut.
- **Passe 9 (J5, format 1.1, migration en avant sans perte)** : lignée **par entrée de couverture** (`coverage[] = {unit, bornes, variant, remote_unit_id, team}`, GAP-9-02) ; chaque empreinte avec sa fonction (`content_hash_by`, `derived_hash`/`derived_by`) ; descripteur de liaison (référence de série distante, sous-source) et clé d'Édition (GAP-9-03) ; une ligne non résolue reste **`unresolved`**, jamais effacée par `meta rebuild` ni par la réadoption. **Entrée non fiable** (GAP-9-10) : taille bornée, schéma strict ; une ligne n'accorde que la lignée, seulement si `file_hash` concorde et si ses ULID sont cohérents, jamais âge, étagère ni visibilité. Droits `0700`/`0600` quand l'uid le permet (GAP-9-15). Aucun miroir sur une racine `adopt_ro` : la lignée est écrite sur la racine `rw` cible au rapatriement (GAP-9-16). Écrit par l'hôte seul (chemin réservé, I37).

### 5.8 Frontière de confiance du contenu (passe 9, J1, EF-BIB-051, ADR-0047)

- **Pourquoi** : le lecteur de livres de Kavita injecte le XHTML d'un EPUB dans sa page sans assainissement, garde ses jetons de session dans `localStorage` et ne pose pas de CSP ; un EPUB récupéré sur le web par un service tiers (lncrawl, flux OPDS de tiers, import) est donc un vecteur d'injection vers les comptes du foyer, enfants et admin compris (précédent : CVE-2024-39307). ENF-SEC-015 ne couvrait que le texte des métadonnées.
- **Déclaration** : `content_trust` au manifeste (défaut `untrusted`), recopié sur la liaison ; l'admin peut abaisser une liaison à `untrusted`, jamais l'élever au-delà du manifeste.
- **EPUB `untrusted`** : à l'ingestion, avant tout export, `xhtml-neutralizer@vN` (fonction unique, versionnée, déterministe) produit le **dérivé canonique** ; l'original garde `content_hash`, le dérivé reçoit `derived_hash` + `derived_by` ; le rendu par destination (OPF, ComicInfo) part du dérivé et donne `file_hash`. L'original reste dans le staging puis dans `/store` quand il existe ; en mode passage, seul le dérivé est rangé.
- **Récupération** : un fichier repris d'une destination est `extracted_derived` (vérifié par `derived_hash`) ; `replica.fidelity = derived` ; `independent_copies()` le compte comme exemplaire lisible ; l'« irremplaçable » de l'original s'évalue sans lui (page Santé : « original perdu, dérivé présent »).
- **CBZ** : entrées d'images en liste blanche (JPEG, PNG, WebP, GIF, AVIF, JPEG XL) et `ComicInfo.xml`, extension = octets magiques ; sinon refus `content.untrusted_entry` (M1a), rastérisation des SVG (M2).
- **Exceptions** : `publisher` et `watermarked` ne sont jamais réécrits hors de l'OPF (ADR-0033).
- **Passe 10 (K9, I45)** : en mode passage, l'**original** d'un EPUB `untrusted` est gardé dans `/store/untrusted/` (sans `/store`, avertissement à la liaison et mode dégradé) ; une **mise à jour de sécurité du neutraliseur** est une migration explicite (ENF-OPS-021) qui régénère chaque dérivé depuis l'original, sinon depuis le dérivé rangé, en tenant `derived_chain` ; `derived_hash` = `content_hash()@v` du dérivé hors OPF, avec `derived_hash_by` ; un CBZ rastérisé (M2) est un dérivé déclaré. Un `watermarked` posé par l'heuristique n'est qu'une suggestion : il ne dispense jamais du neutraliseur, qui ne touche que XHTML et SVG, jamais le tatouage de l'OPF ; seule l'attestation d'achat (`publisher`) dispense. Les EPUB `untrusted` adoptés qui contiennent du contenu actif sont signalés dans le rapport d'adoption (M2).
- **Décodeurs** (U4, ENF-SEC-003) : `Image.open(formats=[…])` sans EPS, `vips_block_untrusted_set`, worker de décodage sans réseau ; pdf.js `isEvalSupported: false` (M5).

### 5.9 Effacement immédiat et pierres tombales (passe 9, J2, EF-BIB-052, ADR-0048)

1. `shelfhold purge --now <œuvre|unité>` (admin, confirmation tapée, audité) calcule l'ensemble des copies gérées par `replica` et les métadonnées associées, puis, **sous intention** (EF-EXP-041) : suppression dans chaque destination (`/library` par `fsops`, destinations M4 par leur contrat), dans `/store`, la corbeille de chaque racine et le staging ; retrait des lignes du miroir, des vignettes et du cache de `/content`, de `unit.json` et de `/data/meta` ; retrait des titres d'`upstream_snapshot` ; retrait de la revendication Suwayomi (passe 12, M7 : en M1a côté Shelfhold seulement, avec l'alerte « supprimer dans Suwayomi » et la copie amont listée au rapport — aucune écriture dans Suwayomi en M1a ; suppression du téléchargement par `release_claim` à partir de M1b) ; scan ciblé de Kavita.
2. Passe outre la corbeille, la mise à l'abri, I11 et la rétention de sauvegarde **pour les copies gérées** ; écrit `content_tombstone` (valeurs brutes de la portée **chiffrées** en AEAD depuis la passe 11 : référence, `content_hash`, `derived_hash` avec leur algorithme ; motif opaque, `epoch`) **et** une ligne dans `<racine>/.shelfhold/tombstones` de chaque racine `rw` (ajout seul, `0600`, chemin réservé, format 2.0 ; une racine `rw` absente reçoit sa ligne au remontage depuis `tombstone_pending_root`, avant toute autre écriture, passe 12, GAP-12-14) ; réécrit `store/INDEX.jsonl` dans la même intention (passe 11, GAP-11-08, M1b).
3. Au démarrage et après toute restauration, le journal hors base est fusionné dans la table **avant** toute réconciliation ou réadoption (I36) ; un fichier retrouvé qui correspond est de nouveau supprimé et signalé.
4. Rapport : copies effacées ; sauvegardes intégrées retenues qui citent le titre ; copies hors de portée (sauvegardes de l'hôte, caches et signets de Kavita, copies hors Shelfhold) à purger par l'admin. Ni la table, ni le journal, ni l'audit ne gardent le titre ni aucun identifiant lisible.
5. **Portées (passe 10, K1, I40)** : une purge d'Unité pose `variant` (référence et contenu) et `place` (clé d'Édition brute, track, segment, numéro brut, part) ; une purge d'Œuvre pose en plus `series_binding` (clé de série amont stable : `sourceId` + `manga.url` pour Suwayomi, jamais un titre) et, en M2, `work` (identifiants externes). Le cycle de suivi ignore une série amont sous pierre tombale (`series.tombstoned_upstream`) ; M1a : alerte « retirer la série dans Suwayomi » ; M1b : `unfollow`.
6. **Journal authentifié et chiffré (passe 10, K2 ; passe 11, L1 ; I41, I46)** : le format 1.1 (HMAC, `salt_id`, empreintes salées par l'identifiant d'instance, lisible dans `.shelfhold-mount-id`) est **remplacé** par le format 2.0 avant toute écriture réelle : chaque ligne porte `seq`, `scope`, `kid` et le chiffré AEAD (XChaCha20-Poly1305, clé HKDF dérivée du trousseau, données associées = format ‖ portée ‖ racine ‖ `seq`) des valeurs brutes. Au rejeu, une ligne qui se déchiffre, ou déjà connue de la table, bloque ; une ligne qui ne se déchiffre pas sous un `kid` **connu** (autre racine, ligne altérée ou forgée) est **inerte** : ni blocage ni suppression, `tombstone.unverified`, ligne au rapport. **Passe 12 (M1, GAP-12-01)** : une ligne dont le `kid` est **inconnu** du trousseau courant (sauvegarde de `/data` antérieure à une rotation) n'est pas inerte : elle bloque `reconcile --adopt-after-restore` et la réacquisition de sa portée (en clair) sur sa racine, émet `tombstone.key_unknown` et l'alerte « réimporter le trousseau », jusqu'à `secrets import` ou la dérogation réservée `tombstone accept-lost-keyring`. Une suppression issue du rejeu après une restauration attend la confirmation de l'admin. Le journal est lu comme une entrée non fiable (taille bornée, schéma strict). Les droits `0600` ne protègent pas d'un uid partagé avec Kavita, d'un partage SMB ni des copies de l'hôte : c'est le chiffrement qui protège la **confidentialité** (aucune attaque par dictionnaire de slugs ni par confirmation d'un `content_hash`) et l'étiquette AEAD l'**intégrité**.
7. **Périmètre (passe 10, GAP-10-09)** : événements, arguments de jobs et journaux structurés ne portent que des ULID (titre résolu à l'affichage par `activity_view`) ; la purge efface aussi jobs Huey, lettres mortes, `notification_pending` et lignes de métadonnées ; après restauration, le rejeu efface les lignes remises par l'instantané. Le rapport cite Suwayomi (téléchargement automatique hors catégorie) et les journaux de l'hôte.
8. **Restes documentés (GAP-10-14)** : un retour arrière de tout le pool par l'hôte (instantané qui contient `/data` **et** `/library`) annule une purge sans que rien ne le détecte ; la doc admin recommande d'exporter le trousseau hors de l'hôte avec `secrets export`.
9. **Test d'appartenance à travers les migrations d'identité (passe 11, L1, GAP-11-01, I46)** : au démarrage, après chaque rejeu et chaque purge, le cœur déchiffre les pierres tombales et construit un index **en mémoire** ; un candidat est comparé après re-normalisation (valeurs brutes de la pierre et du candidat) sous le normaliseur courant et chaque version historique citée ; un fichier importé est haché avec chaque algorithme de `content_hash` cité. Une version citée n'est jamais retirée du code. `normalize migrate` (M1a), la fusion d'Éditions (M2), `migrate_ref` et les alias (M3), `replaced_by` (M6) appellent `tombstone_check` sur leur résultat et sur l'alias inverse ; une correspondance bloque et émet `tombstone.migration_hit`.
10. **Trousseau perdu (passe 11, L1, risque accepté Q64, R62)** : le journal devient inerte ; il ne supprime jamais rien, mais ne bloque plus ; depuis la passe 12 (M1), la perte se **constate** par la dérogation tapée « trousseau perdu », les lignes au `kid` inconnu bloquant jusque-là. `secrets export` (M1a) écrit le trousseau dans un fichier chiffré séparé ; le kit de secours imprimable (M3) ne contient **aucune clé** et **référence** cet export (passe 12, M8) ; `shelfhold status` avertit tant qu'aucun export n'a suivi la première pierre tombale. Une racine reprise (`root takeover`) garde ses pierres tombales lisibles si le trousseau de l'instance prédécesseure est importé, son `root_id` étant conservé (passe 12, M2).
11. **Troncature (passe 11, GAP-11-12, M1b)** : `tombstone_count` et `tombstone_last_tag` recopiés dans la sentinelle et dans la base ; un écart émet `tombstone.journal_truncated` et le journal est complété depuis la table ou une autre racine.
12. **Accès (passe 11, L2, I47)** : `purge --now`, `tombstone lift` et `tombstone list` (seule commande qui déchiffre pour l'affichage) passent par une session fraîche ou la CLI locale, jamais par une clé d'API ni un jeton.
13. **Rotation (passe 12, M1, GAP-12-01 ; précisée en passe 13)** : `secrets rotate` ne réécrit **jamais** le journal (ajout seul) : les nouvelles lignes prennent le nouveau `kid`, les anciennes gardent le leur ; un `kid` cité n'est jamais retiré du trousseau, même après la rétention des sauvegardes, ce qui garde déchiffrables les lignes d'une racine démontée pendant la rotation ; `shelfhold status` avertit après chaque rotation tant qu'aucun nouvel export n'a suivi. La rotation ne rechiffre jamais non plus la table `content_tombstone` (passe 13, GAP-13-10).
14. **Registre des `kid` émis (passe 13, N1, GAP-13-01)** : table `tombstone_kid` en base et champ `tombstone_kids` de la sentinelle de chaque racine `rw`, en ajout seul. Au rejeu, une ligne dont le `kid` est absent du trousseau **bloque** (`tombstone.key_unknown`, `waiting_state` `tombstone_key_unknown`) si ce `kid` figure au registre de la base ou de la sentinelle de sa racine, et reste **inerte** (`tombstone.unverified`, cause `kid_unregistered`) sinon : un `kid` inventé ne suspend plus la réacquisition. Le registre est la seule source du contrôle « `kid` cité jamais retiré », racines démontées comprises.
15. **Racine physique en base (passe 13, N5, GAP-13-04)** : `physical_root` garde le `root_id` de chaque racine ; `root takeover` et la recréation d'une sentinelle perdue le reprennent ; au rejeu, si aucune ligne ne s'authentifie sous le `root_id` courant, les `root_id` connus sont essayés et `tombstone.root_mismatch` est émis.
16. **Rechiffrement après fuite (passe 13, N5, GAP-13-06, M1b)** : `shelfhold tombstone rekey`, action réservée sous intention, réécrit le journal de chaque racine sous un nouveau `kid` (nouveau fichier puis `rename` atomique ; une racine démontée l'est à son remontage, avant toute autre écriture), réécrit la table et `tombstone_pending_root`, et marque l'ancien `kid` compromis (gardé pour lire les anciennes sauvegardes). C'est la seule réécriture permise du journal.
17. **Formats (passe 13, GAP-13-05)** : sentinelle, bail et en-tête du journal portent `schema_version` (ENF-OPS-013) ; une version plus ancienne conserve les champs inconnus en réécrivant.
18. **Purges de M1a reprises en M1b (passe 13, GAP-13-09)** : en M1a, les tickets de téléchargement d'une Unité purgée sont fermés côté Shelfhold ; la migration de M1b déchiffre les pierres `variant` et `series_binding`, liste les copies amont encore présentes et propose `release_claim` ou `unfollow` après confirmation de l'admin.

---

## 6. Pipeline d'export

### 6.1 Étapes
```
déclencheur (unit.stored | work.updated | liaison modifiée | manuel | vérification du rangement)
   │
   ▼
0. GARDES      bail d'instance valide (ENF-OPS-015) ; verrous de ressource Œuvre + dossier de série (EF-EXP-042) ;
               aucune intention ouverte sur les chemins visés (EF-EXP-041) ; série non en pause « autre écrivain » (EF-EXP-043) ;
               sentinelle OK (EF-EXP-029) ; espace disque > seuil (ENF-OPS-006) ; version Kavita dans la plage
               (sinon `degraded_readonly`, aperçu seulement, EF-EXP-009) ; pas en mode maintenance (EF-INT-018) ;
               empreinte d'instance Kavita inchangée (sinon pause, EF-EXP-039) ;
               CLASSEMENT : âge effectif (maximum des Éditions, EF-BIB-037) validé par un admin et ≤ shelf.max_age_rating,
               sinon plan BLOQUÉ « Classer d'abord » (non classé → étagères adultes seulement, EF-USR-015 ; option
               « retenir tant que non classé », EF-EXP-050) ; étagère < 18+ ⇒ destination cloisonnée (EF-EXP-047)
   │
   ▼
1. PLAN        planificateur pur : (liaison, export_items) → état désiré {locator, content_hash, file_hash, sidecar_sha}
               PRÉSENCE (I20) : une Unité couverte par un support présent (unit_coverage) n'est jamais réexportée
               CHEMIN FIGÉ (I22) : chemin calculé depuis export_item.frozen_attrs, jamais depuis l'état courant (§6.14)
               transport ≠ tree → pas de rel_path ; egress = externe + watermarked → refus (EF-EXP-058)
               nom de série : Series si présente, sinon Work ; normalisé (name_normalizer)
               COLLISION : inventaire distant + GET /api/plugin/parse (name + libraryType) + table export_series
                 → plan BLOQUÉ avec proposition de désambiguïsation (EF-EXP-032)
               NOMMAGE : titre de chapitre seulement dans ComicInfo/Title ; aucun marqueur de saison ; collision de numéro
                 avec un fichier existant (adoptés compris, lus via plugin/parse) → remplacer l'adopté ou s'abstenir (EF-EXP-045)
               diff avec le manifeste → ops ordonnées {mkdir, write, rename, recycle, delete, touch_mtime, api_call}
               plan.base_version + empreinte des fichiers (EF-EXP-042)
               changement de chemin → RENAME (jamais supprimer/recréer) (EF-EXP-030)
               assainissement des noms (EF-EXP-013), contraintes de la destination
   │
   ├── aperçu (dry-run) ? → plan + résumé en langage courant (EF-EXP-008) ── fin
   ▼
2. STAGING     construire chaque fichier dans <racine physique>/.shelfhold/staging/<uuid> via openat2 (EF-EXP-059) :
               CBZ (fichier reçu + ComicInfo v2.0/brouillon 2.1 en DERNIÈRE entrée : AgeRating de la table §9 bis,
               SortName, LocalizedSeries) | EPUB passthrough (OPF : série, index, collection, dc:rights ; jamais d'âge, L5 ;
               contenu jamais réécrit, direction et métadonnées d'accessibilité conservées, EF-EXP-033)
               mode passage : le fichier reçu est construit ici puis renommé (aucune copie) ; copie conservée :
               copie indépendante depuis /store (reflink si possible) ; hardlink optionnel (M2) pour le contenu remplaçable seulement (EF-EXP-015, D9)
               une seule Variante par Unité logique, selon la préférence d'équipe (EF-BIB-028, EF-BIB-034)
               EPUB tatoué : seule l'entrée OPF est remplacée, autres entrées copiées octet pour octet (EF-EXP-033)
               fsync(fichier) ; umask de la liaison ; pas de chown
   │
   ▼
3. RENAME      fs_intent(pending) COMMIT → remplacement : ancien → .shelfhold/recycle/<date>/ ;
               renameat(staging → final) [même montage]
               fsync(dossier) ; utimes(dossier de série) (granularité minute de Kavita)
   │
   ▼
4. MANIFESTE   export_record[rel_path] ← (content_sha, sidecar_sha, exported_file_sha, written_by_shelfhold_at,
               state=written) + fs_intent.done dans la même transaction ; événement unit.exported
               option purge_upstream_after_export → « Mettre à l'abri » si nécessaire (EF-BIB-038), puis suppression
               du chapitre téléchargé côté Suwayomi sous intention (B6)
   │
   ▼
5. SCAN        regroupement + debounce par liaison ; Kavita : POST /api/library/scan-folder (chemin traduit
               par path_map) ; un scan par lot pour les plans de renommages ; suppression → scan bibliothèque
   │
   ▼
6. CONFIRMER   M1a : polling (resolve_remote) avec délai max.
               M3 : événements (EF-EXP-034) — Kavita SignalR hub `hubs/messages` (JWT obtenu par
               POST /api/Plugin/authenticate, passé en `?access_token=`), `ScanSeries`/`SeriesAdded`/`SeriesRemoved`,
               progression via la méthode `NotificationProgress` ; Komga `GET /sse/v1/events`
               (`SeriesAdded`/`BookAdded`) ; ABS `POST /api/watcher/update` (scan ciblé par chemin) ;
               délai max puis repli sur polling. → état resolved, lien « Ouvrir dans Kavita »
   │
   ▼
7. VÉRIFIER    M1a réduite : manquant → réexport seulement si copie (ou source) disponible ET écrit par Shelfhold < 24 h,
   (fenêtre de   sinon état missing + question « réexporter / ne plus suivre » ; adopté manquant → signalé ;
   maintenance)  inconnu → signalé, jamais touché ; bloquée si sentinelle KO.
   LE RANGEMENT M3 complète : dérive de hash, orphelins distants, doublons, liens symboliques signalés (EF-EXP-018)
```

### 6.2 Séquence type « nouveau chapitre suivi dans Suwayomi → Kavita » (M1a ; mode piloté et purge en M1b)
1. `schedule` déclenche `acquisition.check_updates` (gigue à graine propre à l'instance, fenêtre horaire en UTC, rendu local). **Amendé passe 3 (C5)** : Suwayomi endormi par Sablier n'exécute pas sa propre planification ; le mode est donc un réglage explicite (`instance.refresh_mode`, EF-SRC-025).
2. **WakeHook** (EF-INT-022) : si Suwayomi est derrière Sablier, l'adaptateur appelle l'API Sablier (`names`/`group`, `session_duration`, stratégie *blocking*) et attend le réveil. Le délai de réveil est distinct, et les réessais de réveil ne comptent pas comme des échecs de source. La session est renouvelée pendant les jobs longs.
3. **Mode piloté** (défaut avec WakeHook) : la méta-source demande à Suwayomi le rafraîchissement des séries suivies (`request_refresh`, rythme poli, gigue, `limit_key`) et attend la fin ; **mode « toujours actif »** : aucune demande, Suwayomi suit sa planification. Dans les deux cas, la **sonde fonctionnelle** (EF-SRC-026) s'exécute : sources chargées, chapitres sortis de la file (réenfilés, plafond), fraîcheur par série, filtres de mise à jour globale. **En M1a** (passe 5, GAP-5-12), seule la fraîcheur par série tourne (k = 3 × la cadence observée, plancher 14 jours, `series.stale` agrégé) : c'est la détection d'un Suwayomi muet avant M1b. Le cycle ne demande que les chapitres au-delà du filigrane `follow_from`, dans la limite du plafond par cycle (EF-EXP-053).
4. La méta-source lit le GraphQL local, rattache chaque chapitre à son **Unité logique** `(édition, track, numéro normalisé, part)` et crée la **Variante** `(source_binding, remote_unit_id)` (EF-BIB-034) ; elle ne demande le téléchargement que pour la Variante retenue par la préférence d'équipe (EF-BIB-035) ; événement `unit.added`.
5. Job `acquisition.fetch_unit` (dedupe_key = variante, attente de l'état « téléchargé », EF-SRC-028, slot `limit_key` appliqué aux **demandes de téléchargement**) : `GET /api/v1/chapter/{id}/download` (contrat vérifié par S5) → CBZ reçu en staging (mode passage) ou dans `/store` (copie conservée), intégrité (CRC, pages) → `unit.state=stored` → `unit.stored`. Si la révision est suspecte (< 50 % des pages, `stubbed`), elle part en quarantaine avec `unit.revision_suspect` (EF-BIB-024).
6. Le consommateur `export` planifie pour la liaison de l'Étagère de l'Édition → étapes 0–6. Si l'option de purge est active, « Mettre à l'abri » s'applique d'abord (EF-BIB-038), puis le téléchargement Suwayomi est supprimé après vérification du hash exporté.
7. `unit.exported` → confirmation d'arrivée : en M1a par **polling** de l'API Kavita après le scan ciblé ; délai dépassé → `target.not_indexed`, Unité « À vérifier » (EF-EXP-034) ; en M3, SSE (Activité : *En préparation → Disponible dans Kavita*) ; résumé quotidien (EF-INT-021).
8. **Relance** (M1b, EF-SRC-026) : seuls les chapitres qui ont une `download_request` ouverte sont réenfilés ; jamais les autres équipes ni l'arriéré non demandé (D7).
9. **Suivre depuis Shelfhold** (M1b, EF-SRC-029) : `follow(series_ref, category)` ajoute la série à la bibliothèque Suwayomi dans la catégorie « Shelfhold » (exclue du téléchargement automatique), ou rattache une série déjà présente ; `unfollow` ne la retire que sur demande explicite.
10. **Plusieurs liaisons et repli** (arbitrage porteur 2, M2, EF-SRC-037, EF-SRC-038, EF-SRC-039, EF-BIB-056) : le cycle de suivi itère par **Édition suivie** ; pour chaque place nouvelle au-delà du filigrane, il applique dans l'ordre pierres tombales, filtres de téléchargement, préférence d'équipe, puis **meilleur rang actif** parmi les liaisons qui ont la place, et n'émet qu'une `download_request` (I52) ; une liaison en panne (`not_loaded`, échecs de contenu, retrait amont) passe inactive avec `disabled_reason` et ses demandes ouvertes sont reportées à la suivante (`source.fallback`) ; les places manquantes entre deux places présentes sont d'abord cherchées sur les autres liaisons, sinon signalées (`series.gap`) ; la prochaine vérification de chaque liaison vient de sa cadence observée (bornes réglables, séries terminées ou en pause rarement vérifiées).

### 6.3 Adoption d'une bibliothèque Kavita existante (EF-EXP-028, M1a ; bibliothèque enfant en M1b)
1. **Audit Kavita en lecture seule** (passe 5, EF-EXP-055) : avant toute adoption ou liaison, lecture des comptes qui ont accès à la bibliothèque ; bibliothèque **exposée aux enfants** → refus en M1a (liste des comptes), pause `target.child_exposure` si l'écart apparaît plus tard.
   La liaison pointe vers une bibliothèque Kavita existante, clé par **chemin relatif** (identifiant Kavita en cache). Inventaire par API et parcours de l'arbre sous `/library`. Le type Kavita et les `ExcludePatterns` existants sont mémorisés (`pre_adoption_settings`).
2. Appariement série ↔ Œuvre/Series et fichier ↔ Unité (nom parsé, ComicInfo, hash). Les éléments appariés sont inscrits au manifeste en état `adopted`, **sans réécriture ni copie** (`content.retention = adopted_ref`). Les `AgeRating` ComicInfo sont convertis par la règle inverse (§9 bis) en **âges suggérés** (`age_suggestion`, provenance « fichier »), jamais en classement (EF-BIB-036). Une bibliothèque marquée « enfants » (ou une étagère au maximum < 18+) envoie ses Œuvres non classées dans « À classer », suivi suspendu, sans déplacement (EF-EXP-048, M1b) ; en M1a, une telle bibliothèque n'est pas adoptée (refus expliqué). **Même numéro** (D10) : un fichier adopté de même numéro qu'une Unité suivie est **conservé et rattaché** comme Variante exportée (`adopted`) ; aucun fichier n'est écrit, aucune traduction n'est changée ; le remplacement reste une proposition. Une automatisation existante qui alimente les dossiers est détectée (§6.8).
3. **Appariement avec le suivi** (EF-EXP-038) : pour chaque série, proposition d'une correspondance Suwayomi (titre normalisé + source) à confirmer, une par une ou en lot (`--pair-auto --min-score`, rapport, confirmation unique) ; une fois liée, les chapitres déjà présents sont associés à leur `remote_unit_id` et ne sont jamais retéléchargés. **Passe 5** : l'appariement (a) **fige le nom de série** Kavita et les champs ComicInfo dominants dans `export_series` (EF-EXP-052) ; (b) pose le **filigrane** `follow_from` = plus haut numéro adopté (EF-EXP-053) ; (c) **sonde les droits d'écriture** du dossier de la série (EF-EXP-054), refus avec la commande de correction sinon. Les doublons de même numéro **déjà présents** sont listés dans le rapport d'adoption (proposition), jamais supprimés (EF-EXP-045).
4. Les écarts au gabarit sont proposés comme **plan de renommages** en aperçu, jamais appliqués seuls. Avant M4, un renommage qui change un chemin ou un nom de série déjà lu affiche l'avertissement de perte de progression (EF-EXP-037) ; la préservation par jeton de chaque membre arrive en M4 (EF-EXP-017/030).
5. Une bibliothèque adoptée n'est jamais modifiée (dossiers, `ExcludePatterns`) sans confirmation. La mise à jour de bibliothèque se fait par GET puis PUT fidèle.

### 6.4 Plans de renommages, retrait, détachement
- **Changement de gabarit, ré-étagement, migration de liaison** (EF-EXP-030) : un **plan de renommages** par lots, avec un scan par lot ; en M3 avec l'avertissement chiffré EF-EXP-037 ; en M4, séquence `pull_progress → move → scans → resolve_remote → push_progress` avec le jeton de **chaque** membre (EF-USR-012). Les dossiers gérés vidés sont supprimés après le scan. Le gabarit étant **copié** dans la liaison, un nouveau défaut est proposé avec aperçu et n'est jamais appliqué seul (EF-EXP-004).
- **Retrait d'une destination** (EF-EXP-031) : soit conserver les fichiers et oublier le manifeste, soit tout retirer (corbeille), avec un avertissement explicite. **Détachement** : retire `.shelfhold/` et la sentinelle, restaure `pre_adoption_settings`, révoque la clé dédiée, laisse un arbre Kavita autonome et produit un rapport des éléments laissés en place.
- **Migration de source** : `shelfhold relink` (M1b) rattache la nouvelle liaison à l'Œuvre existante et ne récupère que les numéros manquants ; détection « Même œuvre ? » en M3 (EF-BIB-029).
- **Suppression** (EF-BIB-021) : trois niveaux (ne plus suivre / retirer des destinations / supprimer le contenu), avec un aperçu d'impact.

### 6.5 Idempotence et cycle de vie
| Cas | Comportement |
|---|---|
| Relance identique | 0 op (manifeste = état désiré), EF-EXP-007 |
| Seules les métadonnées changent | **Pas de réécriture** (EF-EXP-040) : valeur poussée par l'API Kavita (champs de série verrouillés) ; réécriture complète (nouvel inode) seulement sur action explicite « Réécrire le fichier », avec avertissement KOReader et nouvel alias de hash (M4+) |
| Chemin rendu change | `rename` + scans + `resolve_remote` + progression préservée, EF-EXP-017/030 |
| Remplacement (meilleure variante) | Même chemin, ancien en corbeille, EF-EXP-016 |
| Suppression | Fichier en corbeille, scan bibliothèque, dossiers vides supprimés de bas en haut **après** le scan |
| Crash entre rename et manifeste | Intention ouverte résolue au démarrage avant tout job : fichier à la cible avec le hash attendu → manifeste complété, pas de réécriture (§6.7) |
| Deux jobs sur la même série | Sérialisés par les verrous de ressource ; plan périmé refusé (§6.7) |
| Fichier déposé ou supprimé par un tiers dans un dossier géré | Série en pause, alerte, aucun réexport en boucle (§6.7, §6.8) |
| Upgrade automatique (équipe, intégrité, meilleure version) | Même chemin, sous intention ; Activité « position KOReader réinitialisée » (EF-EXP-016) |
| Sentinelle absente | Aucune suppression ni réexport massif ; alerte (EF-EXP-029) |
| Kavita injoignable / clé révoquée | Fichiers écrits, scans mis en attente ; état « Destination injoignable » (§11.2) |
| Kavita réinstallé (empreinte différente) | Pause ; re-résolution par chemin ; aucune recréation ni réécriture (§6.6) |
| Fichier supprimé à la main | Pas de réexport automatique s'il n'a pas été écrit par Shelfhold dans les 24 h : état `missing` + question (EF-EXP-018) |

### 6.6 Liens Kavita par chemin relatif ; réinstallation de Kavita (EF-EXP-039)
- La clé d'une liaison est `(target_id, root_subpath)` ; celle d'une ligne de manifeste, `(shelf_destination_id, export_item_id)` depuis la passe 6 (EF-EXP-058), résolue par `rel_path` pour le transport `tree`. Les identifiants Kavita (bibliothèque, série, chapitre) sont un **cache** ré-résolu à la demande (`resolve_remote`).
- À chaque connexion, Shelfhold compare l'**empreinte d'instance** (`kavita_instance.fingerprint`). Si elle change : exports en pause, `target.instance_changed`, puis assistant « Kavita a changé » : (1) nouvelle clé nommée ; (2) pour chaque liaison, recherche de la bibliothèque dont un dossier racine correspond au chemin traduit, création seulement si aucune n'existe et après confirmation ; (3) réapplication du type et des `ExcludePatterns` ; (4) purge des `remote_ref` ; (5) scans ciblés ; (6) en M4, push de progression.
- Aucun fichier n'est réécrit ni déplacé pendant cette procédure.
- **Mise à jour de Kavita** (version majeure ou mineure) : job d'aperçu qui rejoue `plugin/parse` sur un échantillon du manifeste et produit un rapport de différences (EF-EXP-049).

### 6.7 Journal d'intentions, verrous et instance unique (ADR-0031)

**Instance unique (ENF-OPS-015).** Au démarrage, avant toute autre action : `flock` exclusif sur `/data/.lock` (échec → refus de démarrer en écriture, message explicite) ; puis lecture du bail de chaque racine physique (`<racine>/.shelfhold/lease`, un par racine depuis la passe 7, GAP-7-13) : un bail récent d'une **autre** instance → refus de démarrer en écriture (`instance.lease_conflict`). **Amendé passe 4 (GAP-4-09)** : le bail n'est **pris qu'à l'ouverture d'une session d'écriture** (`instance_id`, battement de cœur toutes les 30 s pendant le lot, validité 3 min) et libéré à la fin ; hors session d'écriture, seul le `flock` sur `/data/.lock` protège l'instance et rien n'est écrit dans `/library`, ce qui laisse les disques durs se mettre en veille (ENF-OPS-014). Le bail est revérifié avant chaque écriture dans `/library`, comme la sentinelle. Ce n'est qu'ensuite que `flush_locks`, la libération des `domain_slot` et des `resource_lock` ont lieu.

**Journal d'intentions (EF-EXP-041).** Toute opération multi-étapes suit : (1) `INSERT fs_intent(state=pending, op, src, dst, expected_sha256, expected_dev_ino)` + `COMMIT` ; (2) `state=applying` ; (3) opérations disque (`renameat`, déplacement en corbeille, suppression, copie vers `/store`, purge Suwayomi) ; (4) mise à jour du manifeste et `state=done` dans **la même transaction**. Au démarrage, avant tout job, chaque intention `pending`/`applying` est résolue en regardant le disque : fichier à `dst` avec le hash attendu → manifeste complété, `done` ; fichier encore à `src` → opération annulée ou rejouée selon l'`op`, de façon idempotente ; ni l'un ni l'autre → `reconcile.drift`, jamais de réexport automatique. La vérification du rangement ignore tout chemin couvert par une intention ouverte (invariant I9).

**Verrous de ressource et plans périmés (EF-EXP-042).** Tout job qui modifie le disque prend `resource_lock(work:<id>)` puis `resource_lock(path:<liaison>/<dossier de série>)` dans cet ordre (pas d'interblocage) ; les verrous Huey (`lock_task`) ne servent qu'à l'idempotence. Un plan porte `base_version` (compteur de version des Unités, du manifeste et des liaisons concernées) et l'empreinte `(chemin, taille, mtime, inode)` des fichiers touchés ; à l'application, toute différence → **412** (`If-Match`, passe 4, GAP-4-16) « l'aperçu a changé, recalculer ».

**Autre écrivain (EF-EXP-043).** Le manifeste, les intentions et l'inventaire du dossier permettent de distinguer : fichier écrit par Shelfhold, fichier adopté, fichier sous intention, fichier **étranger**. Un fichier étranger qui apparaît dans un dossier géré, ou un fichier géré qui disparaît ou change d'inode, **de taille ou de mtime** hors de Shelfhold (modification sur place comprise ; hash comparé pendant la fenêtre de maintenance), met la série (ou l'étagère, au-delà d'un seuil) en pause avec `target.foreign_writer_detected` ; aucun réexport n'a lieu tant que l'admin n'a pas repris la main (`shelfhold series resume`, EF-INT-023).

### 6.7 bis Restauration et époques (EF-INT-026, ADR-0044, passe 7)

1. `shelfhold restore` (M1b) ou la détection au démarrage (copie de `/data` par l'hôte en M1a, retour arrière d'image) pose `instance.restore_epoch` et `restore_generation`.
2. Au démarrage, pour chaque racine physique : génération de la base **=** sentinelle → normal ; **> d'un cran** → arrêt entre le commit et la réécriture de la sentinelle (passe 9, GAP-9-04, I39 : chaque session d'écriture incrémente une fois, base d'abord, sentinelle ensuite) → normal, sentinelle ré-estampillée ; **> de plus d'un cran** → copie divergente → `mount.sentinel_missing` (EF-EXP-029) ; **<** → **`restored_behind`**.
3. En `restored_behind` : aucune écriture, suppression ni scan ; les intentions dont `fs_intent.epoch` est antérieur à l'époque de restauration (compteur, passe 9 ; jamais l'horloge murale, I39) passent `stale_from_backup` (I28) ; le journal de pierres tombales hors base est rejoué (§5.9, I36) ; une réconciliation **en lecture seule** produit un rapport (fichiers rangés depuis la sauvegarde, Unités absentes, intentions périmées, **dérive amont** : séries de la catégorie Shelfhold de Suwayomi inconnues de la base, avec la restauration par défaut proposée quand le relevé manque, passe 9, GAP-9-05) ; `instance.restored_behind` est émis ; `shelfhold status` donne la commande de sortie.
4. `shelfhold reconcile --adopt-after-restore` : réadoption par manifeste, **miroir de provenance de chaque racine** (passe 8, §5.7 : `file_hash` concordant → `exported` avec Variante, Équipe et `export_item`), chemin et `content_hash` (jamais de suppression), acquisitions postérieures signalées, sentinelle ré-estampillée avec la génération courante, retour à l'état normal.
5. Les relevés `upstream_snapshot` restaurés restent valides (I31) ; `detach --restore-upstream` peut s'en servir aussitôt.
6. Passe 9 : les lignes du miroir qui ne se résolvent pas dans la base restaurée (liaison créée après la sauvegarde) restent `unresolved` et sont présentées à l'admin ; aucune n'est effacée (GAP-9-03).

### 6.8 Bascule depuis une automatisation existante (ADR-0034)

Cas typique : Suwayomi télécharge **directement** dans les dossiers de Kavita (montage de son dossier de téléchargement), et des scripts planifiés renomment, dédoublonnent ou posent les couvertures. Laisser Shelfhold tourner à côté produit des doublons (Kavita fusionne deux fichiers de même numéro en concaténant leurs pages), des suppressions croisées et une boucle de réexport.

1. **Détection** (EF-EXP-044) : à l'adoption, motif de nommage de Suwayomi, ComicInfo écrit par Suwayomi, chemin de téléchargement de Suwayomi qui tombe dans le dossier partagé (comparaison avec la correspondance de chemins), fichiers modifiés par un tiers à heure fixe.
2. **Procédure** (`docs/admin/migration-depuis-scripts.md`, générique) : (a) adopter ; (b) apparier adoption ↔ suivi ; (b bis) **rapatrier** (passe 5, EF-EXP-051) : si des fichiers adoptés vivent hors de `/library` (volume de Suwayomi monté dans Kavita, détecté par `statfs` et `(dev, ino)` ; ce volume est monté en lecture seule par une entrée `PathMap` `adopt_ro`, passe 6, EF-EXP-060), dry-run obligatoire puis copie vérifiée par hash au **même chemin relatif** sous `/library`, par le journal d'intentions, scan Kavita et contrôle (nombre de séries et de chapitres, progression) **avant** tout démontage ; si le dossier de téléchargement de Suwayomi est déjà sous `/library`, changer seulement ce dossier dans Suwayomi ; (c) retirer les montages directs et laisser Suwayomi télécharger dans son propre volume (mode passage) ; (d) désactiver les scripts de renommage, de dédoublonnage et de couvertures pour les séries gérées ; (e) garder un éventuel script de mise à jour des extensions tant que la sonde fonctionnelle n'est pas en place, puis le retirer ; (f) activer l'étagère.
3. **Garde** : tant que (b bis), (c) et (d) ne sont pas confirmés, le suivi de l'étagère reste désactivé ; après activation, la détection d'un autre écrivain (§6.7) reste active en permanence.
4. **Retour arrière** (passe 5, EF-EXP-056, ADR-0034 amendée) : `shelfhold detach --restore-upstream` rend les séries à la planification de Suwayomi (catégorie et réglage de mise à jour relevés à l'adoption dans `upstream_snapshot`), arrête le suivi sans toucher aux fichiers et écrit la liste des dossiers gérés pour les anciens scripts ; testé de bout en bout dans la DoD de M1a.

Le plan propre à une installation donnée (chemins, hôtes, noms de scripts) n'est **jamais** dans le dépôt : il vit dans un document privé de l'exploitant.

### 6.9 Romans en cours : politique de tomes (EF-BIB-040)

lncrawl produit un EPUB par tome ou pour tout le roman ; pour un roman en cours, chaque chapitre régénère le dernier fichier, ce qui décale ou perd la progression Kavita. Politique par Édition : `closed_only` (défaut : seuls les tomes clos, de taille fixée par l'Édition, sont exportés), `rolling_tail` (plus un tome « en cours » au nom stable, remplacé au même chemin par le journal d'intentions, avec avertissement de progression), `whole` (romans terminés seulement). Un tome clos est figé : hash stable (invariant I13), jamais régénéré.

### 6.10 Configuration headless de M1a (EF-INT-024)

Ordre type d'une installation sans UI : `shelfhold migrate` → `admin create` → `target add kavita --url --user --accept-admin-risk` (clé nommée à expiration créée et stockée chiffrée) → `source add suwayomi --url --secret-file` (lecture du mode d'authentification, refus de `ui_login` en M1a) → `config set path_map …` (ou `SHELFHOLD_PATH_MAP`) → `config set source.check_mode always_on` → `config set notify.urls …` → `shelf create --max-age 18+ --target kavita --subpath manga` (audit EF-EXP-055) → `adopt --dry-run` puis `adopt`. Le même état s'importe d'un coup par `shelfhold config apply -f shelfhold.toml` (fichier d'état déclaratif au nom libre, distinct de `config.toml` lu au démarrage ; déclaratif, idempotent, `--dry-run` qui affiche les différences ; les secrets restent des chemins `*_file`). Chaque commande a sa route OpenAPI ; l'E2E `test_m1a_headless_install_to_first_chapter_in_kavita` n'écrit ni SQL ni autre fichier que le compose et `shelfhold.toml`.

### 6.11 Fenêtre de maintenance : ordonnancement (ENF-OPS-014, E9)

Fenêtre par défaut **03 h–07 h** (4 h), fixe en M1a, réglable à partir de M1b. Un ordonnanceur de fenêtre exécute, par priorité : (1) checkpoint WAL `TRUNCATE` ; (2) vérification du rangement (≤ 30 min sur le profil de référence) ; (3) comparaison de hash des fichiers gérés (EF-EXP-043) ; (4) intégrité par roulement (≤ 2 h) ; (5) `VACUUM` mensuel ; (6) ramasse-miettes des vignettes (M3). Chaque job avance par **curseur persistant** et vérifie l'heure entre deux opérations : à l'heure de fin, il s'arrête proprement et reprend la nuit suivante ; aucune opération n'est tronquée, aucun job ne déborde (`test_window_jobs_preempted_at_window_end`). La sauvegarde de l'hôte (M1a) ou la sauvegarde intégrée (M1b) est planifiée **après** la fenêtre.

### 6.12 Suwayomi multi-comptes (EF-SRC-031, E12)

- **Détection** : à l'ajout du service connecté et à chaque sonde, lecture du mode d'authentification (`basic_auth`, `simple_login`, `none`, `ui_login`) ; champ `auth_mode` dans l'état de santé.
- **M1a** : `ui_login` → refus clair, état `auth_mode_unsupported`, événement `source.auth_mode_unsupported` ; aucun suivi partiel.
- **M1b** : AuthFlow `SuwayomiJwt` (mutation `login`, jeton d'accès de 5 min rafraîchi, rotation) sur un **compte de service** `shelfhold` aux permissions explicites (téléchargement, gestion de sa bibliothèque ; jamais l'installation d'extensions) ; la bibliothèque qui fait foi est celle du compte de service ; les suivis des autres comptes ne sont jamais lus.
- **Purge** : en `ui_login`, « purger » retire la revendication de Shelfhold sur un téléchargement partagé ; l'espace libéré est mesuré ensuite (stat), « Mettre à l'abri » se décide sur la présence réelle du fichier.
- **Spike S5 étendu** (M0) : contrat joué en `basic_auth` et en `ui_login`, sur la version épinglée et sur la branche principale.
- **Banc de contrat sans contenu tiers** (passe 11, L3, ENF-LEG-013, M0) : le Suwayomi de contrat (S5 local, nightly sur la CI publique) n'a que la **source locale**, alimentée par les CBZ synthétiques du dépôt ; **liste de dépôts d'extensions vide**, vérifiée par une assertion au démarrage du job ; **réseau sortant coupé** (seul le registre d'images épinglé est joignable pendant le tirage, puis réseau interne du job) ; `test_contract_job_has_no_extension_repo_and_no_egress`, `test_contract_suwayomi_uses_local_source_only`. Aucune réponse d'une extension tierce n'entre dans le dépôt ; le corpus d'étiquettes de langue de S5 n'y entre que réduit aux étiquettes (lint `denylist-names`).

### 6.13 Kavita, second écrivain de métadonnées (EF-EXP-057, T6)

| Champ | Propriétaire | Règle |
|---|---|---|
| Fichier, nom de fichier, dossier, ComicInfo | Shelfhold | Écrits par le pipeline ; jamais réécrits pour une simple métadonnée (EF-EXP-040) |
| Nom de série affiché, titres, identifiants externes, couvertures verrouillées | Kavita (et Kavita+) | Relus par l'API avant toute conclusion de collision ou d'appariement ; jamais écrasés |
| Âge de série des bibliothèques gérées, poussé verrouillé par l'API (passe 12, M6) | **Shelfhold**, par son registre `field_owner` (passe 13, N3) | Réécrit seulement s'il figure au registre ; écart avec la dernière valeur poussée ⇒ `reconcile.drift` et nouvelle poussée ; repoussé par `target reconnect` après une réinstallation de Kavita ; la barrière reste la bibliothèque (ADR-0027) |
| Âge Kavita dérivé des tags (depuis 0.9.1.4), autres champs verrouillés par l'admin ou par Kavita+ | Kavita | Jamais réécrits : un verrou absent du registre `field_owner` appartient à un tiers |

La doc admin (`docs/admin/proprietaire-des-champs.md`) reprend ce tableau et recommande de verrouiller ou de désactiver l'écriture de titre par Kavita+ sur les bibliothèques gérées.

### 6.14 Chemin exporté figé (EF-EXP-061, F7, ADR-0040)

Des tomes sont attribués aux chapitres des mois après leur sortie, un chapitre passe en special, un titre est corrigé. Si le nom de fichier dépendait de ces attributs, chaque changement produirait un plan de renommages de masse : perte de progression avant M4 (EF-EXP-037) et déclenchement de la détection d'un autre écrivain (EF-EXP-043).

- **Invariant I22** : à la première exportation, `export_item.frozen_attrs` enregistre la série exportée (figée, EF-EXP-052), le track, `number_norm`, la part et `template_id@version` ; le chemin n'est calculé **que** depuis ces attributs, jamais depuis l'état courant de l'Unité.
- **Attribut changé ensuite** (tome, titre, special) : poussé par ComicInfo à la prochaine réécriture légitime ou par l'API Kavita quand c'est possible (EF-EXP-040, EF-BIB-043) ; sinon il devient une **proposition** de renommage, appliquée seulement sur action explicite.
- **`Vol.X`** n'entre pas dans le nom de fichier d'un chapitre d'une série en cours : le tome n'est écrit que dans ComicInfo (`Volume`), et un golden `plugin/parse` vérifie que Kavita le range correctement (`test_chapter_name_has_no_volume_golden_plugin_parse`). Les tomes clos (`kind = volume`) gardent `Vol.X` dans leur nom (EF-EXP-014).
- **Aucun identifiant Shelfhold** n'est inscrit dans les fichiers exportés (ADR-0040, Q40) : la reconnaissance après restauration ou déplacement repose sur le manifeste, le chemin et le hash (`reconcile --adopt-after-restore`).
- Test de propriétés : `test_late_volume_attribution_never_renames` (Hypothesis : toute suite d'attributions tardives de tome, de special et de titre produit 0 `rename`).

---

## 7. Matrice des destinations

| Destination | Jalon | Layout | Format(s) | Sidecars | Scan / événements | Collections / progression | Règles et pièges |
|---|---|---|---|---|---|---|---|
| **Kavita** | **M1a** | arbre ; 1 dossier par série, rien à la racine ; bibliothèques famille/enfant dédiées (§9 bis) | CBZ ; EPUB/PDF passthrough (M2) | ComicInfo v2.0 + champs du brouillon 2.1 embarqué (`AgeRating` en complément, `SortName`, `LocalizedSeries`) ; OPF : série, index, collection, `dc:rights` (aucun âge dans l'EPUB depuis la passe 11 ; âge de série poussé **verrouillé** par l'API, passe 12, M6) | `POST /api/library/scan-folder` (chemin traduit) ; `GET /api/plugin/parse` (contrôle du **nom** seulement) et `POST /api/Plugin/parse-bulk` (≤ 100 noms, rejeu et adoption, passe 12) ; métadonnées de série (âge verrouillé) ; liste complète dans `docs/kavita-routes.md` ; `library/create`/update GET→PUT avec `ExcludePatterns` ; SignalR `hubs/messages` (M3) | Pull/push de progression en M4 (EF-EXP-026) ; collections M4 | on utilise `ExcludePatterns` des réglages de bibliothèque (la suppression de `.kavitaignore` en v0.7.11 n'est **pas confirmée** par une note de version ; sans impact) ; `scan-folder` exige une **clé d'administrateur** (clé nommée dédiée à expiration, Kavita ≥ 0.9.1, minimum unique depuis la passe 12, M9) ; `plugin/parse` exige `libraryType` et ne lit que la chaîne (ni ComicInfo, ni repli sur le dossier) : le rangement réel est vérifié par le nightly contre Kavita réel ; un 404 sur une route de la liste ⇒ `degraded_feature` (`target.route_missing`, passe 13), jamais de plan sans contrôle de collision ; API sans préfixe de version ni politique de dépréciation (26 routes retirées entre 0.8.9.1 et 0.9.1.4) : test hors ligne de la liste contre l'`openapi.json` figé de chaque version et nightly {minimum, épinglée, dernière}, Kavita de test amorcé sans interface (création de l'admin, puis clé dédiée) ; collections et listes créées sous le compte de service puis promues (M4, passe 12) ; **pièges vécus, convertis en tests golden** (EF-EXP-045) : un marqueur de saison dans le nom de fichier (`{S1 END}`, `[S1 END]`, `- S5 Start`) devient un volume fantôme → titre de chapitre seulement dans ComicInfo ; deux fichiers de même numéro sont fusionnés pages concaténées → garde « même numéro déjà présent » ; chapitre 0 = special, la couverture de série vient du chapitre 1 ; `POST /api/Upload/series` avec préfixe `data:` donne une couverture vide ; couverture AVIF vide et JXL non pris en charge → couverture en JPEG/PNG/WebP (EF-EXP-046) ; identité de série = nom parsé/ComicInfo (deux éditions FR/EN fusionnent → bibliothèques distinctes ou `LocalizedSeries`, collision détectée au plan) ; texte entre `( )` retiré, utiliser `{ }` ; Vol 0 = vrai volume ; specials `SP01` ; pas de champ `Manga` ; bump mtime ; débouncer ; types `Manga=0, Comic=1, Book=2, Image=3, LightNovel=4, ComicVine=5` ; plage de versions supportée (ENF-COMP-001) |
| **Dossier natif / Mihon-Suwayomi local** | M4 | arbre `local/<Series>/` | CBZ/chapitre, EPUB (1 EPUB = 1 chapitre) | `ComicInfo.xml` (Translator = Équipe), `cover.jpg` | aucun | aucun | Aucun `*.json` dans les dossiers de série ; manifeste caché à la racine ; pas de RAR5 (EF-EXP-010) |
| **Komga** | M4 | arbre (tout dossier contenant des fichiers = série) | CBZ, EPUB, PDF | ComicInfo, `cover.*`, `series.json` optionnel | `POST /api/v1/libraries/{id}/scan` (**pas de scan par dossier**), `empty-trash` ; SSE `GET /sse/v1/events` | collections, readlists | Corbeille jusqu'à `empty-trash` (EF-EXP-020) |
| **Calibre-Web-Automated** | M4 | boîte de dépôt | EPUB | OPF embarqué | aucun (inotify/poll) | aucun | Fichiers supprimés après ingestion → suivi par identifiant ; écrire en temp puis rename (EF-EXP-022) |
| **Kobo (fichier + sync)** | M6 | sync émulée | `.kepub.epub` via kepubify | — | — | Kobo sync (EF-INT-012) | Extension `.kepub.epub` obligatoire (EF-EXP-021) |
| **Calibre** | Plus tard | push HTTP par les routes `/cdb/*` du **serveur de contenu** (passe 13, AB2 ; aucun binaire calibre dans l'image) | EPUB, PDF | métadonnées par `/cdb/set-fields` (aucun verrou de champ : `lock` ignoré et déclaré) | aucun ; `add-book` renvoie `book_id` (`locator` `remote_id`) ou `duplicates` | aucun | `add_duplicates = n` comme idempotence ; protocole interne de `calibredb` : plage de versions étroite, nightly épinglé ; compte avec droit d'écriture, 403 = pause ; jamais d'écriture dans l'arbre `Auteur/Titre (id)/` (EF-EXP-023) |
| **Send-to-Kindle** | Plus tard | push e-mail | EPUB (MOBI/AZW plus listés), PDF | — | — | — | ≤ 50 Mo et 25 pièces jointes par e-mail, 200 Mo par le web, expéditeur approuvé, 2FA 48 h si adresse peu sûre ; `idempotency_key`, `can_update/can_delete = false` (EF-EXP-024) |
| **Booklore/Grimmory, Stump** | Plus tard | arbre | EPUB, PDF, CBZ | Stump : ComicInfo v2, OPF homonyme prioritaire, `series.json` | Stump : GraphQL `scanSeries(id)` | — | Stump : motif collection/série **figé à la création**, globs d'exclusion en réglages (plus de fichier d'ignore) (EF-EXP-027) |
| **Audiobookshelf** | Plus tard (réservée) | arbre ; un dossier feuille = un item | M4B/MP3 | `metadata.json`, `cover.jpg` | **`POST /api/watcher/update`** {libraryId, path, type} (scan ciblé) | — | `Auteur/Série/N - Titre {Narrateur}` ; `CDn` fusionnés ; `.ignore` par dossier ; priorité des métadonnées dossier → tags → nfo → txt → opf → metadata.json ; jamais `POST /api/upload` (aucun scan, erreurs masquées) ; `known_bad_versions` < 2.32.2 (CVE-2026-42883, `access_control`) et plancher 2.37 (passe 13) (EF-EXP-035) |
| **KOReader** | M5 | via OPDS + kosync de Shelfhold (V1 : ceux de Kavita) | EPUB, CBZ, PDF | — | — | kosync | Ne jamais supprimer les `.sdr` voisins **[mem]** |

Gabarits par défaut (copiés dans chaque liaison) :
- Kavita manga : `{Series}/{Series} Vol.{Volume:00} Ch.{Chapter:000.#}{ - Title}.cbz`, sans `Vol.` si inconnu.
- Kavita specials : `{Series}/Specials/{Series} SP{n:00} {Title}.cbz`.
- Kavita livres : `{Series}/{Series} Vol.{Volume:00}.epub`.
- Komga : `{Series}/{Series} v{Volume:00} c{Chapter:000.#}.cbz`.
- Mihon local : `{Series}/Ch.{Chapter:0000.#} {Title}.cbz`.

---

## 8. Surfaces d'API

| Surface | Jalon | Description | Auth |
|---|---|---|---|
| REST `/api/v1` | M1a | OpenAPI 3.1 générée par FastAPI ; erreurs **RFC 9457** (problem+json ; passe 10 : type `…/problems/reauth-required` défini dans l'OpenAPI v1, avec `auth_age_s` et `max_age_s`, EF-USR-022 ; passe 11 : `reauth_method`, et type `…/problems/credential-not-allowed` (403) pour une action réservée appelée par clé d'API ou jeton, EF-USR-024 ; passe 12 : `reauth-required` défini dès M1a pour la fraîcheur minimale des actions réservées) dont le `type` est l'URI stable du code du catalogue d'erreurs (même identifiant que l'événement) ; pagination par curseur ; clés d'idempotence sur POST conservées 24 h ; `If-Match`/ETag (412 si périmé, 409 réservé aux conflits métier) ; `202 Accepted` + `Location: /api/v1/jobs/{id}` pour les opérations longues ; `ETag` et `?since=` sur les listes (passe 12, M10, AA4, figé avant 1.0 : jeton **opaque** adossé à la séquence de `event`, réponse avec les **retraits** — suppression, purge, fusion, perte de visibilité pour le lecteur — et problème `…/problems/resync-required` pour un jeton plus ancien que la rétention des événements ; le service worker de la PWA s'en sert pour évincer les copies hors ligne retirées, M5) ; CORS fermé ; dépréciation par `Deprecation`/`Sunset` (au moins une mineure et 6 mois après 1.0) ; champs amont marqués non fiables ; client TS généré vérifié en CI à partir de M3 (EF-INT-001) | clé API scopée hachée (M1a), session admin + CSRF (M1a), sessions multiples et forward-auth avec secret (M3) |
| Surface d'exploitation | M1a | `GET /api/v1/status` (files et séries en pause, raison, commande de sortie) et une route par sortie d'état d'attente (`missing/resolve`, `series/{id}/resume`, `jobs/{id}/retry`, `targets/{id}/reconnect`, `targets/{id}/rotate-key`, `sources/{id}/secret`, `targets/{id}/recheck`, `sources/{id}/recheck`, `works/{id}/rating` en M1b) ; `/healthz` liste les files en pause (EF-INT-023). Passe 5 : configuration headless `POST /api/v1/targets`, `POST /api/v1/sources`, `POST /api/v1/shelves`, `GET\|PATCH /api/v1/config` (EF-INT-024), `POST /api/v1/adoptions/{id}/repatriate` (EF-EXP-051), `POST /api/v1/series/{id}/backfill` (EF-EXP-053), `POST /api/v1/detach` (EF-EXP-056), `GET /api/v1/stats` (EF-INT-025) | session admin ou clé `admin` |
| CLI `shelfhold` | M1a | M1a : `migrate`, `admin create\|create-key\|keys list\|revoke\|rotate\|sessions revoke`, `export --dry-run`, `adopt` (détection d'une automatisation existante, `--confirm-migration`), `intents list\|resolve`, `secrets export\|rotate`, `reconcile`, `status`, `jobs list\|retry`, `missing resolve`, `series resume`, `target reconnect\|rotate-key`, `source set-secret` ; passe 5 (M1a) : `config get\|set\|list\|apply`, `target add\|recheck`, `source add\|recheck`, `shelf create`, `adopt --repatriate\|--pair-auto`, `backfill --gaps\|--all`, `detach --restore-upstream`, `stats` ; M1b : `backup run\|verify\|restore`, `relink`, `follow\|unfollow`, `rate` ; M3 : `admin reset-password`, `export-all`, `maintenance on\|off`, `trash empty` ; M2 : `revisions list\|apply` (EF-BIB-043) ; M4 : `request --from-list` (EF-INT-014, EF-INT-023, EF-INT-024) ; passe 12 : `tombstone accept-lost-keyring` (M1a) | accès local au conteneur : **socket Unix `/data/shelfhold.sock`** (`0600`, uid vérifié par `SO_PEERCRED`) quand le serveur tourne, `flock` de `/data/.lock` sinon ; jamais par TCP (passe 12, M3) |
| SSE `/api/v1/events` | M3 | Flux typé (job.progress, unit.*, export.*, request.*, source.health) ; `Last-Event-ID` = id de la table `event` ; identifiant purgé → `resync_required` (EF-INT-002) ; heartbeat 15 s ; `X-Accel-Buffering: no` ; filtré par contrôle parental (EF-INT-002) | session |
| Origine de contenu `/content/*` | M1a | Couvertures, vignettes, pages ; `Content-Security-Policy: sandbox` par réponse, `nosniff`, `Content-Disposition: attachment` pour SVG/HTML ; vignettes `private, immutable` + ETag + `Vary: Cookie`, autorisation avant le 304 | session / clé |
| `/healthz`, `/readyz` | M0 | Santé ; `{"state":"migrating"}` ; dégradé en JSON ; `/readyz` reste 200 en espace bas (ENF-OPS-003) ; passe 11 (Z1) : **exclus des routes publiées par le proxy inverse** (un seul port est publié ; `/readyz` révèle l'état des montages et du disque), règle et exemple de configuration dans la doc du proxy | réseau interne |
| Notifications Apprise | M1a | URL configurées par l'admin, schémas autorisés ; événements d'exploitation par défaut (EF-INT-006) ; texte amont échappé (ENF-SEC-015) | — |
| Webhooks sortants | M4 | JSON avec `schema_version`, signature HMAC-SHA256 + horodatage, outbox, catalogue `x-webhooks`, double émission en dépréciation ; URL validée par le Fetcher (EF-INT-005) | secret par webhook |
| `/metrics` | M4 | Prometheus opt-in (ENF-OBS-002) | réseau interne ou clé |
| OPDS 1.2 + PSE, OPDS 2.0 | M5 | Navigation, OpenSearch, acquisition ; contrôle parental appliqué (EF-INT-007). En V1, la famille utilise l'OPDS de Kavita | Basic (clé) ou jeton en URL (lecture seule, scopé, haché, rotation, ENF-SEC-013) |
| kosync | M5 | `POST /users/create` (**désactivé**), `GET /users/auth`, `PUT /syncs/progress`, `GET /syncs/progress/:document`. En-têtes `x-auth-user`, `x-auth-key` (= md5 du mot de passe), `accept: application/vnd.koreader.v1+json`. Document = hash partiel KOReader du **fichier exporté** via `document_alias` ; repli d'appariement par champ `metadata` optionnel ; 409 si hash ambigu (EF-LEC-009, ENF-QUAL-005) | clé d'appareil |
| `/lite` | M5 | HTML serveur sans JS pour liseuses sans ES6 (Kobo natif, anciens Kindle) : parcourir, télécharger (EF-LEC-010, ENF-COMP-002) | jeton d'appareil |
| Kobo sync | M6 | Surface minimale : `/kobo/<jeton-appareil>/v1/initialization`, `v1/auth/device` (factice), `v1/library/sync` (NewEntitlement, ChangedEntitlement, ChangedProductMetadata, ChangedReadingState, New/Changed/DeletedTag ; `x-kobo-synctoken` + `X-Kobo-sync: continue`), `v1/library/{id}/state` GET/PUT, téléchargement + kepubify en cache, couvertures, catch-all stub (proxy du magasin en option, fragile). Modèle SyncPoint ; **jeton par appareil** ; protocole non documenté par le fabricant (EF-INT-012) | jeton d'appareil dans le chemin |
| API compatible pour clients tiers | Plus tard | Sous-ensemble lecture seule (EF-INT-011) | clé d'appareil |

---

## 9. Sécurité — modèle de menace

| Actif / surface | Menace | Mitigation | Exigences |
|---|---|---|---|
| Fetcher | **SSRF** (169.254.169.254, LAN, API admin Kavita), rebinding DNS, IP décimales/octales, IPv6 mappée | Client unique httpx + `httpx-secure`, `trust_env=False` ; validation après résolution et à chaque redirection ; IP épinglée ; `lan_targets` explicites ; suite de propriétés ; futurs solveur/headless derrière la même garde | ENF-SEC-001, EF-SRC-003 |
| Plugins | Code malveillant, confusion de dépendances | First-party natifs en V1. Plugins tiers **désactivés par défaut** (`SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS=1` + confirmation tapée « mêmes droits que Shelfhold »). Installation `--only-binary :all: --require-hashes` sans résolution de dépendances, `site-packages` séparé ajouté après celui de l'hôte, refus des noms en conflit. Détecteur `sys.addaudithook` (détecteur, pas sandbox). Aucun secret transmis. M6 : sous-processus JSON-RPC sans réseau | EF-SRC-002, EF-SRC-019, ENF-SEC-009, EF-SRC-016 |
| Index de plugins (M6) | Rollback, gel, index compromis | Signature minisign, `version` **monotone** (anti-rollback), `expires`, sha256 par wheel, révocation, rotation par double signature ; 0 dépôt préconfiguré ; aucun module de retrait de protection listé ; passe 8 (GAP-8-13) : clé de plugin épinglée à `(index, signataire)`, un changement de signataire est une nouvelle installation, secrets non transférés sans confirmation | EF-SRC-017, ENF-LEG-001 |
| XML (OPF, ComicInfo, OPDS, RDF, SVG) | **XXE** (lecture de `/data` et de la clé maîtresse, requête sortante qui contourne le Fetcher), billion laughs | `safe_parse_xml()` unique (import-linter), `defusedxml` ou lxml durci, plafonds ; corpus XXE fichier/http + billion laughs | ENF-SEC-014 |
| Classement | Âge forgé dans un ComicInfo, fournisseur empoisonné, membre sans droit, fusion malveillante qui abaisse l'âge | Seuls les comptes « classer » écrivent `age_rating` ; le reste est `age_suggestion` ; âge effectif = maximum des Éditions ; fusion qui change l'âge jamais automatique | EF-BIB-036, EF-BIB-037 |
| Destinations (M4) | Étagère famille envoyée vers une destination sans cloisonnement d'accès (boîte de dépôt, dossier lu par une tablette d'enfant) | Capacité `access_partitioning` ; liaison refusée si étagère < 18+ et destination `none` | EF-EXP-047 |
| Instances et jobs concurrents | Deux instances ou deux jobs sur les mêmes fichiers (doublons, renommages croisés, manifeste corrompu) | `flock` + bail, verrous de ressource, journal d'intentions, plans périmés refusés | ENF-OPS-015, EF-EXP-041, EF-EXP-042 |
| Fichiers tatoués | Retrait du tatouage (délit), divulgation des données de l'acheteur par un diagnostic ou par une destination qui fait sortir le fichier du foyer | Réécriture ciblée de l'OPF, entrées copiées octet pour octet, exclusion des diagnostics ; planificateur : `watermarked` vers une destination `egress = externe` **refusé**, sans passe-droit (passe 6, F3) ; test dans le kit de conformité des destinations | EF-BIB-041, ENF-LEG-009, EF-EXP-058 |
| Paquet de diagnostic (passe 6) | Nom d'un site non autorisé ou URL d'une série divulgués dans une issue | Domaines, `manga.url` et noms de sous-sources masqués (`<source-1>`, `<domaine-1>`) ; `test_diagnostic_bundle_redacts_source_urls` (GAP-6-22) | ENF-LEG-009, ENF-LEG-004 |
| Notifications Apprise (passe 6) | Apprise fait ses propres requêtes réseau, hors du Fetcher et de la garde SSRF | URL saisies par l'admin seulement ; **schémas restreints par défaut** (liste blanche testée) ; plancher `apprise>=2.1,<3` (passe 11) ; doc « les notifications ne passent pas par la garde SSRF » (GAP-6-21) | EF-INT-006, ENF-SEC-001 |
| Code source (AGPL §13, passe 6) | Utilisateur réseau (famille derrière le proxy) sans accès au code de la version qu'il utilise, y compris modifiée par l'admin | Lien « Code source » (URL configurable, défaut = dépôt + tag de la version) sur la page de connexion neutre et `source_url` dans `/api/v1/about` (M1a), pied de page de l'UI (M3) ; compatible avec la discrétion (aucune balise Open Graph) | ENF-LEG-012, ENF-PRIV-002 |
| Archives importées | Zip-slip, chemins absolus, liens, périphériques, bombes | `safe_open_archive()` unique (import-linter), lecture en flux sans extraction, plafonds par `kind`, `MAX_IMAGE_PIXELS`, worker borné | ENF-SEC-002, ENF-SEC-003 |
| Écritures disque | Lien symbolique planté, évasion de racine, suppression hors arbre | `openat2(RESOLVE_BENEATH\|RESOLVE_NO_SYMLINKS)` (repli `O_NOFOLLOW` + `dev/ino`), `renameat` relatifs ; symlinks refusés ; test de propriétés `test_fsops_never_escapes_root_with_planted_symlinks` | ENF-SEC-012 |
| Racine `adopt_ro` (passe 6) | Écriture ou suppression dans le volume d'un service tiers lu pour l'adoption (corruption de sa bibliothèque) | Montage `:ro` dédié **et** refus dans `fsops` (contrat import-linter, `test_adopt_ro_root_never_written`) ; lecture seulement par `safe_open` sous racine | EF-EXP-060 |
| Racines physiques (passe 6) | Sous-disque non monté sous `/library` pris pour une bibliothèque vidée ; `EXDEV` qui casse l'écriture atomique | Sentinelle, staging et corbeille par racine physique ; refus d'écrire vers une racine absente ; écriture inter-racines par copie vérifiée sous intention | EF-EXP-059 |
| Miroir de provenance (passe 8) | Lignée (clé de source, identifiant distant, équipe) qui sortirait du foyer avec un fichier, ou serait indexée par Kavita | Jamais dans les fichiers exportés ; dossier caché `.shelfhold/` exclu de Kavita (test nightly) ; jamais écrit vers `egress = externe` ni dans un paquet de diagnostic ; aucune URL ni domaine de source | EF-EXP-063, ENF-LEG-009 |
| Contenu exporté (passe 9) | **Relais d'injection vers le lecteur de la destination** : XHTML ou SVG piégé d'un EPUB récupéré sur le web (lncrawl, OPDS de tiers, import), exécuté dans le lecteur de Kavita qui injecte la page sans assainissement et garde ses jetons dans `localStorage` (vol de session, y compris admin) ; entrée SVG ou HTML dans un CBZ | `content_trust` (défaut `untrusted`) ; dérivé neutralisé `xhtml-neutralizer@vN` avant tout export ; liste blanche des entrées CBZ et octets magiques ; corpus d'EPUB piégés ; la défense ne repose jamais sur la seule destination (§5.8, I35) | EF-BIB-051, ENF-SEC-015, ENF-SEC-004 |
| Décodeurs d'images et de PDF (passe 9) | Format exotique délégué à un outil externe (EPS → Ghostscript), décodeur vulnérable, PDF qui exécute du code dans le lecteur | `Image.open(formats=[…])` par `kind`, jamais EPS ; `vips_block_untrusted_set` ; worker sans réseau ; pdf.js `isEvalSupported: false` (M5) | ENF-SEC-003, ENF-SEC-004 |
| Références distantes (passe 9) | Clé d'API dans un lien d'acquisition (OPDS) copiée en clair dans la base, `unit.json`, le miroir du dossier partagé, les journaux et les sauvegardes ; rotation qui recrée le catalogue | Références opaques sans secret, refus ou réécriture `{secret}` par le Fetcher, rédaction des segments de chemin (I38) | EF-SRC-035 |
| Chemins réservés (passe 9) | Destination tierce qui falsifie la sentinelle, le miroir ou les pierres tombales d'une racine | Liste de chemins réservés refusée dans tout `FsOp` ; écrits par l'hôte seul (I37) | EF-EXP-001 |
| Effacement (passe 9) | Contenu à retirer gardé par la corbeille, la mise à l'abri, la rétention ou ramené par le suivi et la restauration | `purge --now`, `content_tombstone` et journal hors base rejoué (I36) ; rapport des copies hors de portée | EF-BIB-052 |
| Montage | Montage absent ou différent → réexport/suppression massive | Sentinelle `.shelfhold-mount-id` **par racine physique** + seuil de manquants → blocage | EF-EXP-029, EF-EXP-059 |
| Texte amont (passe 4) | HTML ou script injecté dans un titre, un résumé ou une description (extension tierce, flux OPDS) : XSS dans l'atelier, reclassement provoqué, **relais d'injection vers Kavita** par ComicInfo `Summary` ou l'OPF, notification piégée | Texte brut par défaut ; nh3 (liste blanche minimale) **à l'ingestion** par `sanitize_upstream_text()` unique (import-linter) ; échappement dans les notifications ; ComicInfo et OPF en texte brut ou HTML assaini ; champs marqués non fiables dans l'OpenAPI ; corpus d'injections | ENF-SEC-015 |
| Contenu servi | **XSS** via SVG/HTML/EPUB, sniffing | Origine/chemin dédié avec `CSP: sandbox` par réponse, `nosniff`, `Content-Disposition: attachment` pour SVG/HTML, couvertures SVG rastérisées ; nh3 côté serveur (ENF-SEC-015) ; CSP par **hash** sur toute page servie (connexion dès M1a, « À classer » en M1b, SPA en M3 ; pas de nonce, pages statiques) ; CSP du lecteur testée en M5 (`font-src`, `media-src`, `frame-src blob:`) | ENF-SEC-004, EF-LEC-005 |
| Forward-auth | Usurpation de `Remote-User` | **Secret partagé obligatoire** `X-Shelfhold-Proxy-Secret` (comparaison en temps constant) + CIDR (/32 conseillé, réseau dédié `shelfhold_ingress`) ; préfixes UI/API seulement, jamais `/opds`, `/kosync`, `/kobo` ; `Remote-User` sur route non éligible → rejet + audit | EF-USR-002 |
| Auth | Bourrage d'identifiants, CSRF, abus des routes publiques ; écran « À classer » ouvert à quiconque atteint l'instance (passe 4) | **Dès M1a** (EF-USR-021) : compte admin local, argon2id, limitation des tentatives, cookies `HttpOnly`/`Secure`/`SameSite=Strict`, jeton CSRF, CSP par hash ; M3 : comptes multiples, limitation par identifiant/jeton et IP réelle (XFF du proxy de confiance uniquement) | ENF-SEC-008, EF-USR-021 |
| Clés d'API (passe 4) | Clé d'amorçage qui fuit (variable d'orchestrateur, journal, historique du shell) et donne la main sur Shelfhold et, par le cœur, sur la clé admin Kavita | Clés **hachées** (préfixe affichable, secret montré une fois), expiration facultative (obligatoire pour `admin` depuis la passe 11), `last_used_at`, révocation et rotation par la CLI, audit ; clé révoquée → 401 | EF-USR-003 |
| Secrets | Fuite (clé admin Kavita, identifiants de sources, cookies) | Chiffrement au repos (clé maîtresse via `SHELFHOLD_SECRET_KEY_FILE`, lisibilité par l'uid vérifiée au démarrage) ; rédaction des logs incluant `apiKey` dans les corps et `x-api-key` ; **clé admin Kavita détenue par le cœur**, compte Kavita dédié **forcément administrateur** (`scan-folder`) : clé nommée à expiration (Kavita ≥ 0.9.1 depuis la passe 12), liste blanche des routes appelées (`docs/kavita-routes.md`), rotation, risque documenté ; jetons en URL seulement pour OPDS/kosync (M5), hachés, lecture seule, rotation ; `shelfhold secrets rotate` ; passe 8 (W2) : enveloppe versionnée `{v, kid, alg, ct}` avec données associées `(table, colonne, id de ligne)`, rotation reprenable après interruption, clé maîtresse hors de `/data` recommandée et avertissement dans `shelfhold status` sinon | ENF-SEC-005, ENF-SEC-013 |
| Visibilité | Contenu adulte ou non classé vu par un enfant via une surface oubliée ou via Kavita | Portée par bibliothèque ; **non classé = invisible** pour les profils enfant et rangé seulement en étagère adulte ; fonction de requête unique ; test paramétré sur toutes les routes OpenAPI, SSE, vignettes, notifications (OPDS/kosync en M5) ; à la lecture dans Kavita, **bibliothèques dédiées** + audit des comptes, l'`AgeRating` exporté en complément (§9 bis) | EF-USR-006, EF-USR-015, EF-EXP-036, ENF-SEC-010 |
| Exposition de l'instance | Indexation, aperçus de liens, empreinte reconnaissable | `X-Robots-Tag: noindex, nofollow`, `robots.txt` « Disallow: / », aucune balise Open Graph, page de connexion neutre (§9 ter) | ENF-PRIV-002 |
| Vie privée entre membres | Demandes ou lectures d'un adulte exposées à l'admin ou aux autres | `visibility` + `activity_view` unique, démasquage journalisé (§9 ter) | EF-USR-016 |
| Forge | Retrait du dépôt ou des images | Second registre, miroir, index configurable (ADR-0029) | ENF-GOV-004 |
| Exports | Écrasement de fichiers non gérés ; second écrivain (script, montage direct) | Fichiers inconnus jamais touchés ; écriture limitée aux racines des liaisons ; collisions bloquées au plan ; autre écrivain → pause | EF-EXP-018, EF-EXP-032, EF-EXP-043 |
| Conteneur | Évasion, élévation, épuisement | uid 1000, `read_only`, `cap_drop: ALL`, `no-new-privileges`, `init`, tmpfs borné, `mem_limit`, `pids_limit` | ENF-SEC-006 |
| Supply chain | Action/image compromise ; jeton de publication volé ; nettoyage du registre qui casse une image multi-arch ou une signature | Paliers : M0 actions épinglées SHA, Renovate (cooldown), rulesets, DCO ; M1b attestations provenance + SBOM (en assets de release), Grype, CodeQL, zizmor ; M3 cosign, Scorecard ; M6 publication de confiance OIDC (aucun jeton longue durée) ; nettoyage GHCR limité aux tags `pr-*` | ENF-SEC-007, ENF-QUAL-007, ADR-0016 |
| Webhooks (M4) | SSRF par URL de webhook, falsification | URL via le Fetcher (garde SSRF) ; HMAC + horodatage | EF-INT-005 |
| Navigateur | Fuite vers des tiers | Aucune ressource tierce (polices auto-hébergées), test Playwright qui refuse toute requête externe | ENF-PRIV-001 |
| Divulgation | Vulnérabilité non signalée ; intégrateur commercial soumis au CRA sans contact ni délais | SECURITY.md (canal dès M0 ; délais, versions supportées et avis avant 1.0), signalement privé GitHub, GHSA ; posture CRA (`docs/legal/cra.md`) | ENF-SEC-011, ENF-LEG-011 |
| Sessions et appareils partagés (passe 10) | Session admin laissée ouverte sur la tablette du salon : un enfant classe une Œuvre adulte « Tous publics », démasque une demande, crée une clé ; jeton d'appareil de l'admin collé dans la liseuse familiale | Sessions de 30 jours glissants par défaut (arbitrage porteur P2) et sessions admin bornées (30 min d'inactivité, 12 h absolues) dans le profil durci ; `reauth-required` sur les actions irréversibles, sur les secrets et, dès qu'un profil restreint existe, sur le contrôle parental (P3) ; passkeys (EF-USR-025, ADR-0050) ; audit `session_id` + `auth_time`, jetons d'appareil au profil lecteur (§9 quinquies, ADR-0049) | EF-USR-022, ENF-SEC-010 |
| Journal de pierres tombales (passe 10, revu passe 11) | Ligne forgée dans `<racine>/.shelfhold/tombstones` par un tiers qui écrit dans le dossier partagé (uid partagé avec Kavita, partage SMB, conteneur compromis) pour faire effacer un contenu irremplaçable | Lignes authentifiées (passe 10 : HMAC ; passe 11 : AEAD, ligne indéchiffrable inerte) ; ligne non vérifiée = jamais de suppression ; suppression au rejeu après restauration confirmée par l'admin ; analyse bornée | EF-BIB-053, EF-BIB-054, ENF-SEC-005 |
| Confidentialité des pierres tombales (passe 11) | Attaque par dictionnaire ou par confirmation (« ce titre a-t-il été purgé dans ce foyer ? ») sur des empreintes salées par un identifiant d'instance public, ou sur un `content_hash` en clair, par quiconque lit le dossier partagé, un partage SMB ou une copie de l'hôte | Valeurs brutes chiffrées en AEAD par une clé dérivée du trousseau, aucune empreinte ni `content_hash` en clair, index déchiffré en mémoire seulement ; `test_tombstone_values_encrypted_not_dictionary_reversible` ; trousseau perdu ⇒ journal inerte (risque accepté) | EF-BIB-054 |
| Clés d'API et jetons (passe 11) | Clé `admin` laissée dans un script domotique ou fuitée : `purge --now` détruit la bibliothèque et en bloque la reconstitution ; changement de `allowed_profiles` ou de la correspondance profil ↔ Kavita qui ouvre la barrière parentale | Axe crédential : actions réservées (définies par propriété depuis la passe 12 : suppression définitive, vider une corbeille, `detach` complet, « tout retirer », effacement de `.shelfhold/`, secrets, contrôle parental, confirmation du rejeu) refusées à toute clé et à tout jeton (403 `credential-not-allowed`), session (fraîche pour les actions à ré-authentification dès M1a, P3) ou CLI locale par socket Unix seulement ; clés `admin` à expiration facultative (proposée à 90 j, obligatoire dans le profil durci — arbitrage porteur P4) ; portée `destructive` réservée | EF-USR-024, EF-USR-003, ENF-SEC-010 |
| Copies hors ligne (passe 12, AA4) | Un livre purgé, reclassé 18+ ou retiré d'une étagère reste dans le cache hors ligne d'un profil enfant, ou dans un fichier téléchargé par OPDS ou `/lite` | PWA (M5) : à la reconnexion, `?since=` (retraits et perte de visibilité) évince le livre du cache du service worker ; fichiers OPDS//`/lite` déjà téléchargés : **non révocables**, risque résiduel accepté (R66), jetons d'appareil révocables et doc famille | EF-LEC-012, EF-INT-001, EF-USR-022 |
| Banc de contrat sur la CI publique (passe 11) | Workflow public qui installe une extension et télécharge un contenu non autorisé, visible dans les journaux d'Actions (scénario de retrait) | Suwayomi de contrat avec la seule source locale et des CBZ synthétiques, liste de dépôts d'extensions vide vérifiée, réseau sortant coupé, assertions dans le job | ENF-LEG-013, EF-SRC-008 |
| Apprise (passe 11) | Service de notification compromis qui renvoie une redirection inter-origine : Apprise renvoie `Authorization`, `X-Gotify-Key`… vers l'hôte cible (CVE-2026-59180) | `apprise>=2.1,<3`, redirections jamais suivies avec les en-têtes d'authentification, API v2 et liste blanche de schémas testées | ENF-SEC-016, EF-INT-006 |
| Tunnels et proxys (passe 11) | Tunnel public (Funnel) qui expose l'instance sans forward-auth ; en-têtes d'identité propres au tunnel (`Tailscale-User-*`) pris pour `Remote-User` ; `/readyz` publié qui révèle montages et disque | Aucun en-tête d'identité accepté hors forward-auth configuré et hors CIDR de confiance, ni sans `X-Shelfhold-Proxy-Secret` quand un secret est configuré (arbitrage porteur P5) (`test_tunnel_identity_header_without_forward_auth_rejected`) ; `/healthz` et `/readyz` exclus des routes du proxy ; doc des tunnels | EF-USR-002 |
| Identités amont (passe 10) | Réinstallation de Suwayomi : identifiants locaux renumérotés, pierres tombales contournées, collisions entre instances | `remote_unit_id` = `sourceId` + URL ; `fetch_handle` en cache ; réinstallation simulée au kit ; `instance_fingerprint` | EF-SRC-036 |
| Dépendances web (passe 10) | Déni de service sur le formulaire de connexion (starlette, python-multipart), `Host` empoisonné, IFD EXIF (Pillow) | Planchers vérifiés en CI ; URL publique configurée ; décodage dans le worker borné | ENF-SEC-016, ENF-SEC-003 |

Le modèle détaillé est maintenu dans `docs/security/threat-model.md`.

---

## 9 bis. Contrôle parental par bibliothèque (ADR-0027)

**Passe 5 (E6).** Dès M1a, avant toute liaison d'étagère ou adoption, un **audit en lecture seule** des comptes Kavita (EF-EXP-055) refuse de ranger dans une bibliothèque **exposée aux enfants** une étagère dont le classement maximal dépasse le palier du compte ; le risque résiduel (configuration des comptes dans Kavita entre deux audits) est attribué au propriétaire, rôle admin de Kavita (`06`, risques résiduels).

**Passe 9 (J3, U1).** La barrière par bibliothèque n'est sûre que si Kavita applique son contrôle d'accès : les avis du 2026-09-05 (versions ≤ 0.9.0.2) montrent un téléchargement de pages et une lecture d'annotations **sans contrôle d'accès par bibliothèque ni restriction d'âge**. `known_bad_versions` porte donc une catégorie ; une version de catégorie `access_control` **bloque** toute étagère à palier < 18+ (`target.child_exposure`), au lieu d'un avertissement ; minimum **0.9.1** pour les étagères famille (ENF-COMP-001, EF-EXP-055). L'agrégation de l'audit se fait par bibliothèque du serveur (`remote_library`, GAP-9-11).

**Passe 10 (K4, GAP-10-04, I43 ; amende J3).** Sur une version `access_control`, tout compte non administrateur atteint **toutes** les bibliothèques de l'instance : la règle de base (refus d'une étagère 18+ sur une bibliothèque exposée aux enfants) s'applique donc à toute l'instance. Si l'audit trouve **au moins un compte restreint** (restriction d'âge, « inclure les inconnus » coupé, accès limité à certaines bibliothèques, ou associé à un profil enfant), **toutes** les étagères liées à cette instance, 18+ et X compris, sont mises en pause avec `target.child_exposure`, dès M1a ; sans compte restreint, avertissement seulement. Sortie : mise à jour de Kavita, puis `shelfhold target recheck`. Même risque hors de portée côté Kavita : une session adulte ouverte sur un appareil partagé (doc famille, EF-USR-022).

**Principe (B1).** La restriction passe par la **bibliothèque**, pas par une métadonnée. L'`AgeRating` ComicInfo et le classement OPF sont des **compléments**, jamais la barrière : un EPUB sans âge lisible, un compte Kavita qui « inclut les inconnus » ou un `AgeRating` mal interprété ne doivent pas suffire à exposer un contenu.

| Couche | Barrière | Exigence |
|---|---|---|
| Étagère | `max_age_rating` + `allowed_profiles[]` ; une Œuvre n'y entre que si son **âge effectif** (maximum des Éditions) est validé par un admin et ≤ maximum (**classer = approuver**, B2, C7, C8) ; non classée → étagères adultes seulement, plan bloqué sinon ; option « retenir tant que non classé » | EF-USR-015, EF-BIB-036, EF-BIB-037, EF-EXP-050 |
| Classement | Seuls les comptes « classer » (admins) fixent l'âge ; âges de fichiers, sous-sources et fournisseurs = **suggestions** avec provenance ; une fusion qui élève l'âge retire l'Œuvre des étagères incompatibles (plan affiché, admin notifié) | EF-BIB-036, EF-BIB-037 |
| Destination | Une étagère < 18+ ne vise qu'une destination `access_partitioning` = `library` ou `user` ; sinon refus | EF-EXP-047 |
| Kavita | Les étagères dont le maximum est < 18+ visent des **bibliothèques Kavita dédiées** (« Manga famille », « Livres famille ») ; les comptes Kavita des enfants n'ont accès **qu'à** ces bibliothèques ; fonctionne pour CBZ **et** EPUB | EF-USR-015, EF-EXP-036 (audit) |
| Fichier | ComicInfo `AgeRating` (table ci-dessous) ; **EPUB : aucun âge écrit** (passe 11, L5, Z3-a : ni `dc:subject`, que Kavita convertit en genre en minuscules et ne lit jamais comme âge, ni `<meta property="shelfhold:age">`, trace de l'outil hors du foyer et préfixe non déclaré) ; à l'adoption, `dc:subject` d'âge et `shelfhold:age` sont **lus** comme suggestions (provenance `file`) ; I49 | EF-EXP-011, EF-EXP-033 |
| Édition nouvelle (passe 11, L4) | Une Édition créée ou rattachée après le dernier classement de l'Œuvre, ou dont un âge suggéré dépasse ce classement, n'entre dans aucune étagère < 18+ (ni rattachement automatique par identifiant externe en M2, ni « Ajouter directement » d'un adulte sans droit « classer » en M3) ; elle part dans « À classer » ; I48 | EF-BIB-055, EF-BIB-037 |
| Surfaces Shelfhold | `visibility_scope` appliquée par une fonction de requête unique ; non classé invisible aux enfants ; recherche « famille » réduite (titre seul + « Demander ») | EF-USR-006, EF-USR-014, ENF-SEC-010 |
| Approbation | Une demande d'enfant ne peut être approuvée qu'avec un âge ; l'écran minimal « À classer » existe dès M1b, derrière le socle d'authentification de M1a (EF-USR-021) | EF-USR-010, EF-USR-015 |
| Adoption | Étagère adoptée au maximum 18+ par défaut ; bibliothèque Kavita « enfants » : Œuvres non classées dans « À classer », suivi suspendu, blocage par Œuvre, « classer tout en lot » avec confirmation | EF-EXP-048 |
| Série Kavita | Rangée selon l'âge **maximal** de ses Œuvres ; reclassement sans changement d'étagère poussé par l'API de métadonnées de série | EF-BIB-025, EF-EXP-040 |

**Passe 11 (L5, Z3-b) — stockage de l'âge.** `age_rating` est un **âge minimal entier** (`0`, `10`, `12`, `13`, `16`, `18`) plus le drapeau `age_x` ; le maximum (I6) et la comparaison avec `shelf.max_age_rating` sont arithmétiques, X étant le plus restrictif ; les libellés passent par le catalogue i18n (un foyer non francophone ne voit jamais de libellé français) ; le palier **12** (CSA, PEGI, FSK) est ajouté ; un palier de plus s'ajoute sans migration.

**Passe 11 (L2) — table de vérité des privilèges, axe crédential (extrait normatif ; la table complète est générée et testée cellule par cellule, `test_truth_table_credential_axis_exhaustive`).**

| Action | Session fraîche (admin) | Session (admin, non fraîche) | Clé d'API `admin` | Clé `request`/`read` | Jeton d'appareil | CLI locale | Membre adulte (session) |
|---|---|---|---|---|---|---|---|
| `purge --now`, `tombstone lift`/`list` | oui | `reauth-required` | **non** (403 `credential-not-allowed`) | non | non | oui | non |
| `shelf.allowed_profiles`, `shelf.max_age_rating`, lier une étagère famille | oui | `reauth-required` | **non** | non | non | oui | non |
| Correspondance profil ↔ compte Kavita (M3) | oui | `reauth-required` | **non** | non | non | oui | non |
| Classer / reclasser | oui | `reauth-required` | **non** | non | non | oui | si rôle « classer », fraîche |
| `root takeover\|reinit`, `store reinit`, `--accept-admin-risk` | oui | `reauth-required` | **non** | non | non | oui | non |
| Clés et jetons (créer, tourner, révoquer), `secrets export`/`rotate` | oui | `reauth-required` | **non** | non | non | oui | ses propres clés `read`/`request` (M3), fraîche |
| Démasquer une demande privée | oui | `reauth-required` | **non** | non | non | oui | non |
| Vider la corbeille, `detach` complet, « tout retirer » d'une destination, confirmer une suppression issue du rejeu, `tombstone accept-lost-keyring` (passe 12, M4) | oui | `reauth-required` | **non** | non | non | oui | non |
| Ajouter directement vers une étagère famille (M3) | oui | oui | oui (Édition classée seulement) | non | non | oui | Édition classée seulement (I48) |
| Approuver une demande, lancer un suivi ou un rattrapage | oui | oui | oui | `request` : créer une demande au nom du sujet | non | oui | ses demandes |
| Lire le catalogue | oui | oui | oui | `read` : au nom du sujet | au nom de `subject_user_id` (I44) | oui | oui |

**CLI locale (passe 12, M3)** : seul le socket Unix `/data/shelfhold.sock` contrôlé par `SO_PEERCRED` donne la colonne « CLI locale » ; une requête HTTP, même sur `127.0.0.1` ou par un tunnel du même espace de noms réseau, est évaluée comme une session ou une clé. **Fraîcheur minimale (passe 12, M5)** : dès M1a, la colonne « Session (non fraîche) » renvoie `reauth-required` pour toutes ces actions (`auth_time` ≤ 900 s (arbitrage porteur P3)). **Passe 13 (N2, N5)** : la règle vise les actions **déclenchées par un crédential** ; la liste fermée des émetteurs système (staging orphelin, sonde, corbeille au-delà de 30 j, garde disque) en est exemptée, limitée aux chemins créés par l'hôte ; le socket refuse un pair dont `SO_PEERCRED.pid` vaut 0 (hors de l'espace de noms PID du conteneur : hôte, autre conteneur) sauf `SHELFHOLD_CLI_ALLOW_HOST=1`, journalisé ; l'uid de Shelfhold sur l'hôte vaut un admin (R67) ; en M6, aucun sous-processus de plugin ne voit `/data` ni le socket.

Derrière le forward-auth (M3), une session sans mot de passe local n'est jamais « fraîche » pour une action réservée (`reauth_method = local_password`) ; en OIDC (M5), `prompt=login`, `max_age` et claim `auth_time`.

**Table de correspondance figée (B4), testée en golden.**

| Âge Shelfhold | ComicInfo `AgeRating` écrit | Valeurs ComicInfo lues à l'adoption → âge Shelfhold |
|---|---|---|
| Tous publics | `Everyone` | `Everyone`, `G`, `Early Childhood`, `Kids to Adults` |
| 10+ | `Everyone 10+` | `Everyone 10+`, `PG` |
| 12+ (passe 11) | `Teen` (valeur la plus proche **supérieure**) | `PEGI 12`, `12+` (suggestion) |
| 13+ | `Teen` | `Teen` |
| 16+ | `Mature 17+` (valeur la plus proche **supérieure** : l'Œuvre est cachée à un compte limité à 16 ans dans Kavita, documenté) | `MA15+`, `M`, `Mature 17+` |
| 18+ | `Adults Only 18+` | `Adults Only 18+`, `R18+` |
| X | `X18+` | `X18+` |
| non classé | aucun `AgeRating` | `Unknown`, `Rating Pending`, absent |

**Recherche famille (B3).** `source_config.family_safe` est posé par l'admin (Gutenberg, Wikisource, BnF… et le catalogue déjà classé). Pour un profil enfant, seules ces sources sont interrogées ; un résultat non classé est projeté en `{title}` seul, sans couverture ni résumé, avec l'unique action « Demander ».

**Audit Kavita (GAP-2-05, C10).** En lecture seule, dès M3 grâce à la correspondance **déclarative** profil ↔ compte Kavita (nom d'utilisateur saisi sur le profil, sans jeton, EF-USR-017) : utilisateurs Kavita, restriction d'âge, option « inclure les inconnus », bibliothèques accessibles ; alerte si un compte associé à un profil enfant voit une bibliothèque dont le classement maximal dépasse l'âge du profil. Schéma conseillé : **une bibliothèque Kavita par palier d'âge utilisé** (ex. 10+ et 13+ sous le même toit), accès cumulatif. Le cas « OPDS Kavita avec un compte restreint » fait partie de la checklist M3.

## 9 ter. Discrétion et vie privée (ADR-0030)

- **Discrétion de l'instance** (ENF-PRIV-002, M1a) : le middleware d'en-têtes de sécurité (déjà responsable de la CSP) ajoute `X-Robots-Tag: noindex, nofollow` à toutes les réponses ; `robots.txt` renvoie `Disallow: /` ; aucune balise Open Graph ni Twitter Card ; la page de connexion est neutre (titre générique, aucune couverture ni liste de bibliothèques) ; toute route de contenu exige l'authentification, un lien collé dans une messagerie n'ouvre que la page de connexion.
- **Vie privée entre membres** (EF-USR-016, M1b pour le modèle) : `visibility` (`private` | `household`) sur les demandes et les événements. Défaut `private` pour un adulte, `household` pour un profil enfant (visible des parents ; le contrôle parental prime). **Une seule** fonction `activity_view(viewer, event) -> EventView | None` projette chaque événement vers l'Activité, le SSE, les notifications et le résumé quotidien ; l'admin voit les opérations (job, octets, erreur) avec le titre masqué pour les demandes privées d'autres adultes ; le démasquage est explicite et journalisé (`audit`). Table de vérité rôle × visibilité × type d'événement testée.
- **Charges utiles sortantes** (passe 7, GAP-7-16) : webhooks et outbox (M4) passent par `activity_view` avec un rôle « intégration », testé dans la table de vérité ; le titre d'une demande privée n'en sort jamais.
- **Vocabulaire** (ENF-LEG-006) : « bibliothèque personnelle du foyer », jamais « partager » au sens de diffuser.
- **Rétention et pseudonymisation** (ENF-OPS-009, EF-USR-013) : événements et jobs 90 j, audit 1 an ; à la suppression d'un utilisateur, l'audit garde un jeton opaque.

## 9 quater. Domaine public par pays et fichiers tatoués (ADR-0032, ADR-0033)

- **Pays de l'instance** (`instance.jurisdiction`, EF-SRC-024) : déduit de la locale (FR par défaut pour une locale `fr-FR`), affiché et confirmé dans l'assistant (Q32). Chaque source déclare `rights_basis` et `rights_jurisdiction` ; Gutenberg et Standard Ebooks sont « libres de droits aux États-Unis » (ENF-LEG-008). Depuis la passe 6 (F11, ADR-0042), les dates de vie sont portées par le **contributeur** (`contributor.death_date`, date partielle) et le calcul porte sur **toutes** les contributions à droits propres (`contribution.role` : auteur, illustrateur, traducteur, adaptateur) ; les champs `author_death_year`, `translator`, `translator_death_year` de l'Édition ne sont plus qu'un cache dérivé. Pour le droit américain, c'est l'année de **première publication** de l'Œuvre (`work.first_published`) qui compte. Le calcul indicatif (vie + 70 ans ; prorogations de guerre françaises ; + 30 ans pour un auteur « mort pour la France » ; droits propres de la traduction) produit un **avertissement**, jamais un blocage, et n'est pas un avis juridique (Q27).
- **Fichiers tatoués** (EF-BIB-041, ENF-LEG-009) : le tatouage relève des informations sur le régime des droits (CPI L331-11) et contient des données personnelles de l'acheteur. Détection heuristique à l'import (entrées ou métadonnées typiques, saisie de l'utilisateur) → `content.watermarked`. Conséquences : OPF seul réécrit, autres entrées copiées octet pour octet (I14) ; exclu des paquets de diagnostic, des fixtures, des issues ; jamais exporté vers une destination hors du foyer : le planificateur refuse toute liaison dont la destination déclare `egress = externe` (passe 6, F3, EF-EXP-058).

## 9 quinquies. Sessions, ré-authentification et appareils partagés (passe 10, K5, ADR-0049)

- **Pourquoi** (Y1) : la barrière parentale repose sur l'identité de l'appelant. Sur un appareil partagé, une session admin ouverte ou un jeton émis au nom de l'admin donne à un enfant le droit de classer (classer = approuver, ADR-0027), de démasquer ou de créer une clé, sans aucune faille logicielle, et l'audit attribue le geste à l'admin.
- **Session (M1a)** : `auth_time`, `last_seen_at`, `absolute_expiry`, `remember` ; **arbitrage porteur P2** : 30 jours glissants pour tous les rôles, « se souvenir de moi » coché ; le profil durci (`docs/admin/durcir.md`) rétablit pour l'admin 30 min d'inactivité, 12 h absolues et « se souvenir de moi » refusé sans confirmation ; identifiant renouvelé à la connexion et à la ré-authentification ; valeurs dans la référence de configuration.
- **Fraîcheur minimale (M1a, passe 12, M5 ; liste réduite par l'arbitrage porteur P3)** : les **actions à ré-authentification** — `purge --now`, `tombstone lift`/`list`, `secrets export`/`rotate`, `root`/`store reinit`, vider la corbeille, `detach` complet, confirmation de suppression post-restauration, et le contrôle parental seulement si un profil restreint existe — exigent `auth_time` ≤ 900 s, sinon `reauth-required` ; mot de passe ou passkey (M1b, ADR-0050).
- **Fraîcheur (M1b)** : colonne `fresh_auth_s` de la table de vérité (ENF-SEC-010) ; liste de P3 par défaut (900 s) ; démasquer, créer ou révoquer une clé ou un jeton, gérer les profils (M3) ne demandent de fraîcheur que dans le profil durci ; classer ou reclasser et enregistrer une passkey la demandent dans tous les profils (amendé le 2026-11-11, décision du porteur, voir `docs/decisions/M1b.md`) ; sinon 401 `application/problem+json` de type `…/problems/reauth-required` (RFC 9457, dans l'OpenAPI v1) que l'UI traite par une demande de mot de passe en gardant l'action.
- **Audit** : `session_id` et `auth_time` sur chaque action soumise à la fraîcheur ; une contestation se résout par session et par appareil.
- **Axe crédential (passe 11, L2, I47, EF-USR-024)** : la fraîcheur n'a de sens que pour une session ; une clé d'API n'a ni `auth_time` ni confirmation humaine. Les actions réservées (table de §9 bis) sont donc **refusées à toute clé d'API, portée `admin` comprise, et à tout jeton d'appareil** (maintenu par P4), et passent seulement par une session (fraîche pour la liste de P3) ou la CLI locale ; la liste `fresh_auth_s` s'étend à `purge --now`, `tombstone lift`, `allowed_profiles`, la correspondance profil ↔ Kavita, `root`/`store reinit`. Clés `admin` à expiration facultative (arbitrage porteur P4) ; clés `read`/`request` avec `subject_user_id` (M3) ; forward-auth : mot de passe local exigé pour les actions réservées ; OIDC (M5) : `prompt=login`, `max_age`, `auth_time`.
- **Jetons d'appareil** (crochet M3, M5) : `subject_user_id` (profil lecteur) ≠ `issued_by` (admin) ; l'appairage demande le profil sans défaut ; les filtres s'appliquent au profil lecteur (I44).
- **Passkeys (M1b, arbitrage porteur P7, ADR-0050, EF-USR-025)** : connexion sans mot de passe et ré-authentification en un geste ; mot de passe en repli ; table `webauthn_credential` ; contexte sécurisé exigé (HTTPS ou `localhost`).
- **Profil durci (arbitrage porteur P1, ENF-SEC-008)** : `security.profile = hardened` rétablit les sessions admin courtes, la fraîcheur sur classer/démasquer/clés, l'expiration obligatoire des clés `admin` ; jamais exigé au démarrage.
- **Hors de portée** : une session adulte de Kavita ouverte sur la tablette commune ; la doc famille recommande un compte Kavita par lecteur sur tout appareil partagé. Pas de codes de récupération web : kit de secours imprimable et second admin facultatif (EF-USR-023).

---

## 10. Déploiement

### 10.1 Compose de référence (homelab, déployé par un outil GitOps)

Shelfhold est **toujours actif**, jamais placé derrière Sablier. Il **réveille** Suwayomi par le port WakeHook (ADR-0025) ; depuis la passe 6 (GAP-6-18), le WakeHook se rattache à **toute dépendance**, source ou destination : si Sablier endort aussi Kavita, Shelfhold le réveille avant `scan-folder` et l'audit d'EF-EXP-055, avec le même délai de réveil distinct. Les images sont publiées sur GHCR **et** sur un second registre (ADR-0029) ; le compose documente les deux noms. Aucun profil lncrawl n'est fourni : l'utilisateur pointe Shelfhold vers sa propre instance. Aucune variante d'image `:full` n'est publiée en V1 (ENF-PERF-002).

```yaml
# compose.yaml — seul fichier présent sur le serveur (+ .env). Le compose Kavita existant n'est PAS modifié.
services:
  shelfhold-migrate:
    image: ghcr.io/<org>/shelfhold:0.2           # tag X.Y épinglé (0.Y avant 1.0) ; l'outil GitOps applique les patchs
                                                 # second registre équivalent : <registre2>/<org>/shelfhold:0.2 (Q28)
    command: ["shelfhold", "migrate"]           # sauvegarde pré-migration + Alembic ; refuse un schéma plus récent
    user: "1000:1000"
    read_only: true
    environment: &env
      TZ: Europe/Paris
      SHELFHOLD_SECRET_KEY_FILE: /run/secrets/shelfhold_secret_key
    secrets: [shelfhold_secret_key]
    volumes: &vols
      - /srv/shelfhold/data:/data                 # SSD conseillé, local (base, config, unit.json)
      - /srv/media/shelfhold-store:/store         # copies conservées (copies indépendantes) ; HDD possible ; même FS que /library pour le reflink
      - /srv/media/kavita:/library                # parent EXISTANT des médias Kavita (exemple)
      # - /srv/media/suwayomi-downloads:/upstream/suwayomi:ro   # BASCULE SEULEMENT (passe 6, EF-EXP-060) : PathMap adopt_ro,
      #                                                         # lu pour adopter et rapatrier, jamais écrit ; à retirer ensuite
    restart: "no"

  shelfhold:
    image: ghcr.io/<org>/shelfhold:0.2
    container_name: shelfhold
    depends_on:
      shelfhold-migrate: { condition: service_completed_successfully }
    user: "1000:1000"
    # group_add: ["<gid partagé avec Kavita>"]   # si nécessaire ; pas de chown
    init: true
    read_only: true
    tmpfs: ["/tmp:size=256m"]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 768m                               # budget : ≤ 250 Mo repos / ≤ 500 Mo pic (ENF-PERF-001)
    pids_limit: 256
    stop_grace_period: 60s
    environment:
      <<: *env
      SHELFHOLD_UMASK: "0002"
      SHELFHOLD_PATH_MAP: "/library/manga=/manga;/library/books=/books;/library/comics=/comics"
      # pendant la bascule seulement : …;/upstream/suwayomi=/suwayomi:adopt_ro   (EF-EXP-060)
      # forward-auth désactivé par défaut (P5) ; à activer seulement derrière un proxy d'authentification :
      # SHELFHOLD_FORWARD_AUTH: "true"
      # SHELFHOLD_TRUSTED_PROXY_CIDRS: ${PROXY_IP}/32           # obligatoire si forward-auth activé
      # SHELFHOLD_PROXY_SECRET_FILE: /run/secrets/shelfhold_proxy_secret   # recommandé, facultatif
      SHELFHOLD_WAKEHOOK_SABLIER_URL: http://sablier:10000      # optionnel (EF-INT-022)
      SHELFHOLD_WAKEHOOK_SABLIER_NAMES: suwayomi
      SHELFHOLD_WAKEHOOK_SESSION: 30m
      # SHELFHOLD_WAKEHOOK_SABLIER_NAMES peut aussi viser kavita (passe 6, EF-INT-022 : WakeHook pour toute dépendance)
      SHELFHOLD_MAINTENANCE_WINDOW: "03:00-05:00"  # jobs disque groupés (ENF-OPS-014)
      SHELFHOLD_SUWAYOMI_REFRESH_MODE: piloted      # piloted | always_on (EF-SRC-025)
      SHELFHOLD_JURISDICTION: FR                    # pays de l'instance (EF-SRC-024, défaut déduit de la locale)
      # SHELFHOLD_PLUGIN_INDEX_URL / SHELFHOLD_UPDATE_CHECK_URL : configurables, vérificateur désactivé par défaut (ADR-0029)
      MALLOC_ARENA_MAX: "2"
    secrets: [shelfhold_secret_key]                 # + shelfhold_proxy_secret si forward-auth avec secret
    volumes: *vols
    healthcheck:
      test: ["CMD", "shelfhold", "healthcheck"]   # appelle /readyz
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 60s
    logging: { driver: json-file, options: { max-size: "10m", max-file: "3" } }
    restart: unless-stopped
    networks: [shelfhold_ingress, media]

secrets:
  shelfhold_secret_key:   { file: ./secrets/shelfhold_secret_key }
  # shelfhold_proxy_secret: { file: ./secrets/shelfhold_proxy_secret }

networks:
  shelfhold_ingress: { external: true }           # réseau dédié proxy ↔ Shelfhold (recommandation du profil durci, P6)
  media:             { external: true }           # Kavita, Suwayomi, Sablier
```
Notes :
- **Honnêteté du déploiement** (ENF-OPS-005) : le cas de référence, c'est 1 conteneur Shelfhold + les services tiers déjà présents. Budget documenté : Shelfhold (≤ 250 Mo au repos) + Suwayomi (≥ 750 Mo) + Kavita (~200 Mo, pics de 2 à 2,7 Go lors des scans).
- Image de base `python:3.13-slim`. Mitigations mémoire : `MALLOC_ARENA_MAX=2`, plafond du cache vips, imports paresseux.
- Les secrets doivent être lisibles par l'uid 1000. C'est vérifié au démarrage, avec un message explicite en cas d'échec (ENF-SEC-005).

### 10.2 Démarrage
0. **Verrou d'instance** : `flock` exclusif sur `/data/.lock` ; échec → refus de démarrer en écriture (ENF-OPS-015).
1. Lecture de la config (env > `config.toml` > base).
2. **Refus de démarrer si le schéma de la base est plus récent** que le code (ENF-OPS-002). Migrations faites par `shelfhold migrate` ; `/healthz` renvoie `{"state":"migrating"}` le cas échéant.
3. Vérifications :
   - secrets lisibles ;
   - `/data` local (pas NFS/CIFS) ; `/store` présent si une étagère est en copie conservée ;
   - espace disque ;
   - racines physiques de `PathMap` sondées (`statfs`, `st_dev`, capacités ; EF-EXP-059) ; sentinelle de **chaque** racine physique (compteur de génération) ; racines `adopt_ro` ouvertes en lecture seule (EF-EXP-060) ; et **bail d'instance** de chaque racine (`<racine>/.shelfhold/lease`, passe 7) (bail récent d'une autre instance → refus, `instance.lease_conflict`) ;
   - sonde d'écriture et de lecture Kavita (EF-UX-002) ;
   - version Kavita dans la plage supportée ; version Suwayomi (`aboutServer`) dans la plage supportée (ENF-COMP-001).
4. **Résolution des intentions ouvertes** (`fs_intent`, §6.7) avant tout job ; comparaison des projections `meta/` avec la base et réparation (ENF-OPS-020) ; purge des staging orphelins ; `flush_locks` Huey, libération des baux `domain_slot` et des `resource_lock` (ENF-OPS-007).
5. Chargement des plugins : refus des `api` incompatibles, shim N-1, tiers désactivés par défaut.
6. Démarrage des consommateurs Huey (`io`, `cpu`, `maint`), du planificateur (rattrapage : **une** exécution par périodique en retard d'après `last_run_at`, Huey ne rattrapant rien lui-même) et du bus d'événements ; comparaison de l'empreinte d'instance Kavita (§6.6).
7. `/readyz` = 200.

### 10.3 Arrêt
`SIGTERM` (via `init`) :
- plus aucune réclamation de tâche ;
- les tâches en cours atteignent un point de reprise (au chapitre) ;
- fermeture des flux SSE ;
- checkpoint WAL ;
- libération du bail d'instance ;
- sortie avant `stop_grace_period` (ENF-OPS-007). Un redéploiement qui recrée le conteneur ne perd rien : aucune écriture hors volumes (`read_only`), toute planification vit dans Shelfhold, jamais dans un cron ou un timer de l'hôte (ENF-OPS-017).

---

## 11. Observabilité et pannes

### 11.1 Signaux
| Signal | Implémentation | Jalon |
|---|---|---|
| Logs | structlog JSON en conteneur, lisible en dev ; contexte `request_id`, `job_id`, `source_key`, `plugin_id`, `shelf_destination_id` ; rédaction des secrets (ENF-OBS-001) | M0 |
| Santé | `/healthz` (vivacité, état de migration) ; `/readyz` (base inscriptible, migrations, sentinelle, espace `/data` et `/library`) ; healthcheck Docker avec `start_period` (ENF-OPS-003) | M0 |
| Événements d'exploitation | `backup.failed`, `backup.stale`, `disk.low`, `job.dead_lettered` (agrégé), `target.unreachable`, `target.version_unsupported`, `reconcile.drift`, `secret.undecryptable`, `mount.sentinel_missing`, `source.broken`, `source.not_loaded`, `source.downloads_dropped`, `series.stale`, `source.version_unsupported`, `target.key_expiring`, `target.foreign_writer_detected`, `instance.lease_conflict`, `unit.safeguard_failed` ; passe 4 : `target.instance_changed`, `target.forbidden`, `target.not_indexed`, `source.auth_expired`, `wake.deferred`, `metadata.provider_down`, `unit.safeguard_backlog`, `notify.failed` (bannière) → Apprise par défaut (EF-INT-006, ENF-OBS-005) ; chaque événement est aussi un code d'erreur stable (EF-UX-003) ; ajouts passe 5 : `target.permission_denied`, `target.child_exposure`, `source.auth_mode_unsupported` | M1a ; passe 7 : `instance.restored_behind` |
| Page « Santé » | État des dépendances, dernière sauvegarde, espace disque, version Kavita, sources, **nombre d'Unités irremplaçables**, dont celles **sans copie conservée**, et âge de la dernière sauvegarde de `/store` déclarée, audit des comptes Kavita (EF-EXP-036), santé fonctionnelle de Suwayomi (dernier téléchargement réussi, séries figées), écarts de nommage après mise à jour de Kavita (EF-EXP-049), copies ×3 signalées | M3 |
| Santé des sources | Dernier succès, latence, taux d'erreur, code d'erreur du catalogue | M3 |
| Audit | Approbations, changements de visibilité, plugins (ENF-OBS-003) | M3 |
| Métriques | `/metrics` Prometheus opt-in : jobs par état et durée, fetch par domaine et statut, attentes de limitation, exports, scans (ENF-OBS-002) | M4 |
| Traces | Crochets OpenTelemetry désactivés par défaut (ENF-OBS-004) | Plus tard |
| Activité (UI) | Famille : un état (*En préparation · Disponible dans Kavita · En attente*) ; admin : étapes, « À vérifier », pilotage de la file (EF-UX-010, EF-INT-017) | M3 |

Budgets mesurés sur le **profil de référence** (2 000 Œuvres, 60 000 Unités, 3 M pages, 2 To, 500 séries suivies, 6 utilisateurs ; générateur synthétique, ENF-PERF-008). **Passe 5 (GAP-5-18)** : les budgets de métadonnées se mesurent **en CI** sur le profil CI (fichiers creux, quelques Gio, référence versionnée, non bloquant) ; débit disque, intégrité et RSS en pic se mesurent **hors CI** sur le profil complet avant chaque release, preuve en asset. Tous les jobs de fenêtre tiennent dans **4 h** (§6.11) :
- vérification complète du rangement ≤ 30 min ;
- intégrité par roulement ≤ 2 h par nuit ;
- vérification des nouveautés de 500 séries ≤ 20 min ;
- RSS ≤ 250 Mo au repos et < 500 Mo pendant un export de 500 chapitres (ENF-PERF-001, ENF-PERF-009).

### 11.2 Matrice de pannes des dépendances (ENF-OPS-011)
Principe : une **panne d'infrastructure met en pause** la file concernée. Elle ne produit pas d'« échecs » et ne consomme pas de réessais (EF-UX-003) ; elle ne compte jamais comme erreur de contenu (EF-BIB-042). **Alignement passe 4 (D11)** : chaque ligne porte un événement d'EF-INT-006, qui est aussi le code d'erreur (EF-UX-003), et, si un humain doit agir, une commande de sortie (EF-INT-023) ; `test_failure_matrix_events_and_codes_consistent` vérifie la correspondance dans les deux sens, en M1a pour les lignes de M1a.

| Dépendance | Détection | Comportement | Signal / action proposée |
|---|---|---|---|
| Kavita injoignable | timeout/5xx sur scan ou parse | Fichiers écrits et manifeste à jour ; scans et confirmations mis en attente puis rejoués ; aucune suppression | `target.unreachable` ; « Kavita ne répond pas — vérifier le conteneur » |
| Clé Kavita révoquée | 401 | Destination en pause ; aucun appel ; plan seulement | `target.unreachable` ; « Reconnecter Kavita » (`shelfhold target reconnect`, assistant en M3) |
| Compte Kavita rétrogradé | 403 sur `scan-folder` ou une route de la liste blanche | Destination en pause ; fichiers déjà écrits conservés | `target.forbidden` ; « rendre au compte dédié ses droits d'administrateur » |
| Fichier écrit mais jamais indexé | confirmation par polling (M1a) ou événement (M3) absente après le délai | Unité « À vérifier », demande liée jamais laissée « En préparation » ; aucun réexport | `target.not_indexed` (exclusion, type de bibliothèque, surveillance de dossiers) |
| Version Kavita hors plage | lecture de version | `degraded_readonly` : aperçu seulement, aucune écriture ; version vulnérable connue : avertissement | `target.version_unsupported` |
| Route Kavita absente (passe 13, N4) | 404 sur une route de `docs/kavita-routes.md` | `degraded_feature` : fonctions dépendantes coupées ; sans `plugin/parse` ni `parse-bulk`, aucun plan appliqué, aperçu « non vérifié » ; `waiting_state` `route_missing` | `target.route_missing` ; `shelfhold target recheck` |
| Kavita réinstallé | empreinte d'instance différente | Exports en pause, cache d'identifiants purgé, aucune recréation | `target.instance_changed` ; `shelfhold target reconnect` (M1a), assistant « Kavita a changé » (M3, §6.6) |
| `/store` absent après restauration | copies conservées introuvables | Aucune suppression ; Unités `store_missing` | `reconcile.drift` ; « Ré-adopter puis re-télécharger » |
| Suwayomi endormi (Sablier, M1b) | connexion refusée + WakeHook configuré | Réveil, attente bornée, réessais non comptés comme échecs ; lot différé si le réveil échoue | `wake.deferred` après 3 lots différés |
| Suwayomi en panne | timeout après réveil | File de la méta-source en pause, reprise automatique ; aucune erreur de contenu comptée | `source.broken` agrégé |
| Suwayomi refuse l'authentification | 401 / `AuthExpired` après `refresh` | Service en pause ; aucune erreur de contenu comptée, aucune mise à l'abri | `source.auth_expired` ; `shelfhold source set-secret` |
| Schéma GraphQL de Suwayomi changé | `SchemaMismatch` | Suivi en pause ; aucune erreur de contenu comptée | `source.version_unsupported` (cause « schéma ») |
| File de mise à l'abri saturée (M1b) | quota d'EF-BIB-042 dépassé | Lots plafonnés ; purge refusée pour les Unités en attente | `unit.safeguard_backlog` |
| Suwayomi joignable mais muet | sonde fonctionnelle (EF-SRC-026) | Source « dégradée », chapitres abandonnés réenfilés (plafond), avertissement bloquant si un filtre global saute les séries | `source.not_loaded`, `source.downloads_dropped`, `series.stale` |
| Suwayomi hors plage de versions | `aboutServer` | Suivi en pause pour ce service, message dédié | `source.version_unsupported` |
| Seconde instance | `flock` ou bail | Refus de démarrer en écriture | `instance.lease_conflict` |
| Restauration ou retour arrière (passe 7) | génération de la base < sentinelle d'une racine, ou `restore_epoch` nouveau (EF-INT-026) | `restored_behind` : lecture seule, rapport, intentions `stale_from_backup` jamais rejouées | `instance.restored_behind` ; `shelfhold reconcile --adopt-after-restore` |
| Autre écrivain | fichier étranger ou disparu dans un dossier géré | Série/étagère en pause, aucun réexport | `target.foreign_writer_detected` |
| Clé Kavita proche de l'expiration | date d'expiration | Rotation guidée | `target.key_expiring` (J-14, J-1) |
| Instance lncrawl injoignable (M2) | timeout | File lncrawl en pause | `source.broken` |
| Source amont limitée (429) | `Retry-After` | Attente, slot libéré (ni pause ni action humaine) | — (journal) |
| SMTP / Apprise en échec (résultat par service de l'API v2 `AppriseResult`, passe 11) | erreur d'envoi | Réessai borné ; notifications agrégées au prochain résumé ; jamais bloquant | `notify.failed` : bannière « Notifications en échec » |
| API de métadonnées (quota / panne) (M2) | 429, quota journalier, panne prolongée | Seau à jetons respecté ; enrichissement différé ; cache servi | Indicateur sur la fiche ; `metadata.provider_down` après 24 h |
| Disque bas | < 5 % ou < 10 Gio sur `/data`, `/store` ou `/library` (reprise à ≥ max(8 %, 15 Gio), passe 5) | Files en pause, pas de réessais ; lecture et UI maintenues ; `/readyz` reste 200 (dégradé) | `disk.low` ; bannière ; « Vider la corbeille » |
| Montage absent/différent | sentinelle | Vérification, suppressions et scans bloqués | `mount.sentinel_missing` |
| Racine physique absente sous `/library` (passe 6) | `st_dev` de la racine égal à celui du parent, ou sentinelle de la racine absente (EF-EXP-059) | Écritures, suppressions et scans refusés **vers cette racine** ; les autres racines continuent | `mount.sentinel_missing` (racine en cause) ; « monter le disque, puis `shelfhold target recheck` » |
| Kavita endormi par Sablier (passe 6, M1b) | connexion refusée + WakeHook configuré pour la destination (EF-INT-022) | Réveil avant `scan-folder` ou l'audit ; attente bornée ; scans différés, aucun `target.unreachable` pendant le délai de réveil | `wake.deferred` après 3 lots différés |
| Clé maîtresse illisible | échec de déchiffrement | Démarrage refusé pour les fonctions à secret ; message explicite | `secret.undecryptable` |
| Destination de sauvegarde absente | échec d'écriture | Sauvegarde marquée en échec ; rétention locale non purgée | `backup.failed` / `backup.stale` |
| Dossier de série non inscriptible (passe 5) | préflight à l'appariement ou `EACCES` à l'écriture (EF-EXP-054) | Série refusée ou en attente `permission_denied` ; pas de réessai en boucle | `target.permission_denied` ; commande de correction ; `shelfhold target recheck` |
| Bibliothèque Kavita exposée aux enfants (passe 5) | audit en lecture seule (EF-EXP-055) | Liaison refusée ou étagère en pause ; aucune écriture dans Kavita | `target.child_exposure` ; « retirer l'accès du compte dans Kavita » ; `shelfhold target recheck` |
| Suwayomi en mode multi-comptes (passe 5) | mode d'authentification lu (EF-SRC-031) | M1a : service refusé ou en pause ; M1b : compte de service | `source.auth_mode_unsupported` ; `shelfhold source recheck` |
| Suwayomi ou lncrawl réinstallé (passe 10) | empreinte d'instance différente (EF-SRC-036, M1b) | Service en pause, aucune cascade « série retirée », aucune mise à l'abri, tickets orphelins purgés | `source.instance_changed` ; `shelfhold source reconnect` |
| Racine d'une autre instance, vide ou restaurée en retard (passe 10) | sentinelle d'un autre `instance_id`, absente, ou en retard de plus d'un cran (EF-EXP-064) | Écritures, suppressions et scans vers la racine bloqués ; `.shelfhold/` jamais effacé | `mount.sentinel_missing` (cause) ; `shelfhold root takeover\|reinit\|restored` (M1b) |
| Ligne indéchiffrable dans le journal de pierres tombales (passe 10, revu passes 11, 12 et 13) | rejeu au démarrage ou après restauration : AEAD invalide sous un `kid` connu, `kid` absent du registre des `kid` émis (ligne forgée, passe 13), ou trousseau perdu après la dérogation (EF-BIB-053, EF-BIB-054) | Ligne **inerte** : ni blocage ni suppression ; ligne au rapport (cause `kid_unregistered` pour un `kid` inventé) | `tombstone.unverified` ; décision de l'admin |
| Ligne au `kid` enregistré mais absent du trousseau (passe 12, M1 ; registre, passe 13) | rejeu : `kid` absent du trousseau, présent au registre de la base ou de la sentinelle (EF-BIB-053) | Réadoption et réacquisition de la portée bloquées sur la racine (`tombstone_key_unknown`) ; rien n'est supprimé | `tombstone.key_unknown` ; `secrets import` ou `tombstone accept-lost-keyring` |
| Journal rattaché à un autre `root_id` (passe 13) | rejeu : lignes authentifiées sous un `root_id` connu de `physical_root` seulement (EF-BIB-054) | Lignes appliquées sous ce `root_id` ; rien n'est supprimé | `tombstone.root_mismatch` ; rapport |
| Journal de pierres tombales tronqué (passe 11, M1b) | `tombstone_count`/`tombstone_last_tag` de la sentinelle ou de la base ≠ journal (EF-BIB-054) | Journal complété depuis la table ou une autre racine ; aucune suppression | `tombstone.journal_truncated` |
| Migration d'identité qui rencontre une pierre tombale (passe 11) | `tombstone_check` dans `normalize migrate`, fusion d'Éditions, `migrate_ref`, alias, `replaced_by` (EF-BIB-054) | Ligne migrée bloquée, rien n'est réacquis | `tombstone.migration_hit` ; décision de l'admin |
| Résolveur DNS filtrant du foyer (passe 11) | réponse `0.0.0.0` ou `::` pour le domaine d'une source (EF-UX-003) | Source en pause, aucune erreur de contenu, pas classé en attaque SSRF | `source.dns_blocked` ; « autoriser le domaine dans le bloqueur DNS » |

---

## 11 bis. Thèmes du front (arbitrage porteur 2, ADR-0051, EF-UX-012)

- **Socle unique** : un seul jeu de composants (shadcn-svelte / bits-ui, ADR-0013) et d'écrans ; les quatre directions de `front-propositions.html` sont des **thèmes** : A « Bibliothèque » (défaut), B « Console », C « Galerie », D « Calme ».
- **Un thème = des jetons + des variantes déclarées.** Jetons au format W3C Design Tokens (`frontend/src/lib/themes/<thème>/tokens.json`), compilés en propriétés CSS sous `:root[data-theme="…"]` et `[data-color-scheme="dark"]` ; variantes prises dans une **liste fermée** de points de mise en page (`density`, `cover_layout` grille ou liste, `series_header`, `nav_width`) lus par les composants comme des propriétés CSS ou des options typées, jamais comme un nom de thème (I51).
- **Choix** : `user.theme` (défaut instance réglable), `color_scheme` `auto` par défaut ; appliqué avant le premier rendu (attribut posé par le script de démarrage, CSP par hash inchangée) pour éviter le flash.
- **Contrôles** : script de contraste AA par thème en clair et en sombre (bloquant, ENF-A11Y-001) ; captures Playwright des écrans clés par thème ; règle d'analyse statique « aucun composant ne lit le nom du thème ».
- **Jalons** : mécanisme et A en M3 (1.0, Q18 respectée : une seule direction livrée à la 1.0) ; B, C, D et le sélecteur en M4, coupables par la règle des 150 % ; `/lite` (M5) reste hors thèmes.

## 12. Index des ADR

| ADR | Sujet | Statut |
|---|---|---|
| ADR-0001 | Enregistrer les décisions d'architecture (MADR 4) | accepté |
| ADR-0002 | Langage et runtime : Python 3.13+ / FastAPI en bordure | accepté, amendé passe 1 |
| ADR-0003 | Monolithe modulaire hexagonal, frontières import-linter réduites | accepté, amendé passes 1, 3, 4 et 6 (points d'entrée XML et texte amont uniques ; aucun symbole de service hors de son adaptateur, `adopt_ro`, `independent_copies`, `normalize_lang`) |
| ADR-0004 | SQLite WAL + SQLAlchemy/Alembic, FTS5 (+ trigram), SQLite seul | accepté, amendé passes 1 et 3 (entretien, unicité sans NULL) |
| ADR-0005 | File de jobs : Huey SQLite épinglé + couche Shelfhold | **accepté** (sous réserve du spike S1), amendé passe 2 (rattrapage) ; passe 11 : trois classes de priorité fixes, rattrapage en lots |
| ADR-0006 | API REST + OpenAPI 3.1 + SSE | accepté, amendé passes 1 et 4 (412, 202, idempotence 24 h, dépréciation, CORS) ; passe 13 : retraits de `?since=` limités à ce que le lecteur a vu |
| ADR-0007 | Contrat de plugin et isolation progressive | accepté, amendé passe 1 |
| ADR-0008 | Méta-sources (Suwayomi, lncrawl) plutôt qu'embarquer la JVM | accepté, amendé passes 1, 2, 3, 4, 5 et 6 (port `ConnectedService`, ADR-0041) (vérification pilotée, sonde fonctionnelle, filtrage par équipe ; GraphQL, `follow`/`unfollow`, relance bornée, 401 ; multi-comptes `ui_login`, fraîcheur en M1a) |
| ADR-0009 | Modèle unifié et identité | accepté, amendé (voir ADR-0021) ; passe 6 : ULID opaques, la base fait autorité |
| ADR-0010 | Stockage par Unité, sans CAS en V1 | accepté, amendé passe 1, **amendé par ADR-0028** ; passe 6 : `replica`, `independent_copies` ; passe 7 : `content_hash`/`file_hash`, `/store` = original ; passe 9 : état `extracted_derived`, dérivé neutralisé |
| ADR-0011 | Contrat de destination d'export | accepté, amendé (voir ADR-0022) ; amendé passe 3 (`access_partitioning`, `accepted_image_types`) ; passe 6 (`transport`, `egress`, `accepted_content_types`) ; passe 7 : `egress` par instance, liaison `pull` ; passe 8 : recouvrement d'arbres refusé, `series_grouping`, `FsOp` ; passe 10 : `unlink` réservé à l'hôte, `can_purge`, `readback`, `render()`, `write_inline`, espaces de noms des plugins ; passe 11 : références d'un `write` limitées au plan ; passe 13 : `ApiCall.body`, `push` réservé jusqu'à M4, Calibre par `/cdb/*`, Audiobookshelf sans upload |
| ADR-0012 | Lecteur intégré, secondaire, en M5 | accepté, amendé passe 1 ; passe 7 : position par fichier ; passe 9 : pdf.js `isEvalSupported: false` |
| ADR-0013 | Front SvelteKit 3 (SPA), repli React 19 | **accepté** (sous réserve du spike S2) |
| ADR-0014 | Licence AGPL-3.0-or-later + SDK MIT | accepté, amendé passes 1, 2, 4 et 6 (lien vers le code source, AGPL §13 ; exception provisoire dès le premier commit public, ADR-0036) ; passe 11 : `sdk/` sous MIT avec en-têtes SPDX, le SDK n'importe jamais le cœur |
| ADR-0015 | Posture légale | accepté, réécrit passe 1, amendé passes 2, 3, 4 et 5 (renvoi vers ADR-0037, CRA ; avis juridique en deux temps Q27a / Q27b) ; passe 9 : admission des plugins de métadonnées, proxy jamais présenté comme contournement, effacement immédiat |
| ADR-0016 | CI/CD et supply chain par paliers | accepté, amendé passes 1, 2 et 4 (cache dans le registre, preuves en assets, nettoyage `pr-*`, OIDC, DCO) ; passe 10 : planchers de sécurité des dépendances vérifiés en CI (ENF-SEC-016) ; passe 11 : banc de contrat Suwayomi sans contenu tiers (ENF-LEG-013), planchers Apprise et Komga, frontière de licence du SDK outillée |
| ADR-0017 | Stratégie de tests (TDD), fixtures synthétiques | accepté, amendé passe 1 ; passe 11 : contrat Suwayomi réel sur la source locale seule, sans réseau sortant |
| ADR-0018 | Authentification : admin local + clés + forward-auth facultatif (CIDR obligatoire, secret recommandé, P5) ; briques Python (`argon2-cffi`, `webauthn`, `Authlib`), better-auth écartée (P8) ; OIDC en V2 | accepté, amendé passes 1, 4 et 5 (socle d'authentification en M1a, clés hachées à expiration ; fait Suwayomi corrigé : comptes utilisateurs fusionnés) ; passe 10 : champs de session, durées admin bornées, renvoi à ADR-0049 ; passe 11 : clés `admin` à expiration facultative (arbitrage porteur P4), aucune action réservée par clé, en-têtes d'identité de tunnel refusés sans secret |
| ADR-0019 | Nom du projet : Shelfhold | proposé |
| ADR-0020 | Positionnement : Shelfhold est l'atelier, Kavita le lieu de lecture | accepté, amendé passes 2 et 4 (revue étendue à Suwayomi et aux concurrents) ; passe 13 : registre `field_owner`, modes `degraded_readonly`/`degraded_feature`, `target.route_missing` |
| ADR-0021 | Entité Series, définition de l'Édition et identité des Unités | accepté, amendé passes 2, 3 et 6 (identité à deux niveaux, équipe par série, âge par Édition, Œuvres liées, politique de tomes ; `text_version`, couverture, traduction relais) ; passe 7 : Équipe par Édition, provenance de la traduction ; passe 8 : révision à identifiant égal, `number_segment` ; passe 10 : `performance` dans la clé d'Édition, identité amont des chapitres |
| ADR-0022 | Montage du parent existant, correspondance de chemins et sentinelle | accepté, amendé passes 2, 3, 4, 5 et 6 (racines physiques, `adopt_ro`, identité de fichier ; seuil de la sentinelle par liaison d'étagère avec plancher ; préflight des droits par dossier) (génération de sentinelle, bail, autre écrivain, pièges de nommage ; bail limité aux sessions d'écriture, gabarit par fichier, 403, « jamais indexé ») ; passe 7 : bail par racine, `/store` et racines ; passe 9 : incrément du compteur de génération (base, puis sentinelle), chemins réservés ; passe 10 : sorties `root takeover\|reinit\|restored`, identifiants d'instance prédécesseurs ; passe 11 : prédécesseurs lus par les `kid` du trousseau importé, compteur de pierres tombales dans la sentinelle ; passe 13 : `physical_root`, sentinelle versionnée |
| ADR-0023 | Sauvegarde, restauration et export intégral en format ouvert | accepté, amendé passes 2, 4 et 5 (périmètre `/data` ; sauvegarde intégrée en M1b, outil de l'hôte en M1a ; RPO 24 h quotidien en M1a, export de clé au format age) ; passe 7 : époque de restauration ; passe 9 : phrase de passe hors de l'arbre sauvegardé, trousseau complet, purge immédiate des copies gérées ; passe 10 : `schema.sql` et dictionnaire, `store/INDEX.jsonl`, export des suivis OPML et CSV, `export-all` sans contenu sous pierre tombale |
| ADR-0024 | Contrat de données du SDK v1 et compatibilité `api` N-1 | accepté, amendé passes 4 et 6 (parcours de l'auteur de plugin, migrations d'état ; `LanguageTag`, `RANGED_CONTENT`, `ConnectedService`, transport des destinations) ; passe 7 : `RangeRef`, `translation_nature`, aller-retour JSON ; passe 8 : plan d'opérations fichier sérialisable, clé de plugin épinglée au signataire ; passe 9 : `content_trust`, références sans secret, chemins réservés des `FsOp` ; passe 10 : stabilité des identités à la réinstallation de l'outil connecté, valeurs attestées des plugins tiers, compléments du contrat de destination ; passe 11 : type `AgeRating` entier, sorties des états propres, attestation des plugins non first-party dès M2 ; passe 13 : `ApiCall.body` facultatif (ajout compatible), `push` refusé par le kit avant M4 |
| ADR-0025 | Réveil des services à la demande : port WakeHook (Sablier) | accepté, amendé passes 3 et 6 (vérification pilotée ; toute dépendance, destination comprise) |
| ADR-0026 | Gouvernance, politique de versions et pérennité | accepté, amendé passes 2, 3, 4 et 5 (budget de temps, pas de promotion avant 1.0, dépréciation de l'API ; statut public, usage de l'IA par le mainteneur, renvoi vers ADR-0038) |
| ADR-0027 | Contrôle parental par bibliothèque | accepté (passe 2), amendé passe 3 (autorité de classement, âge effectif, adoption enfant, cloisonnement des destinations), amendé passe 4 (M1b, âge par Édition explicite), amendé passe 5 (audit Kavita en lecture seule dès M1a) ; passe 9 : `known_bad_versions` `access_control` bloquant pour les étagères famille, minimum 0.9.1 ; passe 10 : instance `access_control` avec compte restreint → toutes les étagères en pause ; fraîcheur d'authentification pour classer ; passe 11 : Édition non classée hors des étagères < 18+, âge entier minimal + drapeau X, palier 12, aucun âge écrit dans un EPUB, axe crédential ; passe 13 : âge de série au registre `field_owner`, repoussé après réinstallation de Kavita |
| ADR-0028 | Mode passage et disposition du stockage (`/data`, `/store`, `/library`) | accepté (passe 2), amendé passes 3, 4, 5 et 6 (registre des exemplaires, lecture des œuvres non exportées ; « Mettre à l'abri » ; copie indépendante, jamais de lien dur ; comptage par source amont et quota ; `unit.json` dans `/data` dans tous les modes, rattrapage vers la copie conservée) ; passe 9 : mise à l'abri de l'original des sources `untrusted`, purge immédiate prioritaire ; passe 10 : original `untrusted` gardé dans `/store` en mode passage |
| ADR-0029 | Résilience à un retrait sur la forge | accepté (passe 2) |
| ADR-0030 | Discrétion de l'instance et vie privée entre membres | accepté (passe 2), amendé passes 3, 4 et 6 (aucun identifiant dans les fichiers exportés, paquet de diagnostic expurgé ; rétention, pseudonymisation, vignettes privées ; en-têtes en M1a) ; passe 8 : miroir de provenance hors des fichiers, jamais vers l'extérieur ; passe 9 : droits du miroir et des pierres tombales, aucun titre ; passe 11 : pierres tombales chiffrées, aucune empreinte lisible |
| ADR-0031 | Journal d'intentions, verrous de ressource et instance unique | accepté (passe 3), amendé passes 4 et 5 (bail pendant les sessions d'écriture, taille et mtime ; garde de migration, schéma d'intention gelé, opération « rapatrier ») ; passe 7 : intentions `stale_from_backup` |
| ADR-0032 | Domaine public apprécié par pays | accepté (passe 3), amendé passe 6 (toutes les contributions, dates sur la personne) |
| ADR-0033 | Fichiers tatoués : préserver, ne jamais diffuser hors du foyer | accepté (passe 3), amendé passe 6 (refus vers `egress = externe`) ; passe 9 : `publisher` par construction, jamais neutralisé |
| ADR-0034 | Bascule depuis une automatisation existante | accepté (passe 3), amendé passes 4, 5 et 6 (montage `adopt_ro` ; fichier adopté de même numéro conservé et rattaché ; rapatrier avant démontage, nom de série figé, `detach --restore-upstream`, réversibilité corrigée) |
| ADR-0035 | Découpage de M1 (M1a/M1b) et budget de temps par jalon | accepté (passe 4), amendé passes 5 et 6 (intouchables et coupables de M1a, boîte ré-estimée à 200 h ; ajouts de schéma de la passe 6 absorbés, Q42) ; passe 7 : ajouts de schéma, Q44 |
| ADR-0036 | DCO et politique de contribution ; exception de plugin dès le premier commit | accepté (passe 4) |
| ADR-0037 | Posture face au Cyber Resilience Act | accepté (passe 4) ; passe 9 : date d'application de l'art. 14 |
| ADR-0038 | Validation du besoin et portes de décision | accepté (passe 5) |
| ADR-0039 | Formats sur disque versionnés et migrations en avant | accepté (passe 5), amendé passe 6 (la base fait autorité, arborescence plate par ULID, `sha256:`) ; passe 7 : normaliseurs versionnés ; passe 8 : format du miroir de provenance ; passe 10 : journal de pierres tombales 1.1 signé, miroir 1.2, `schema.sql` ; passe 11 : journal de pierres tombales 2.0 chiffré, remplaçant 1.1 avant toute écriture ; passe 13 : sentinelle, bail et en-tête du journal versionnés |
| ADR-0040 | Couverture des Unités, `export_item` et chemin exporté figé | accepté (passe 6) ; passe 7 : `export_series` ULID, bornes de couverture ; passe 8 : provenance par miroir, annexe dans l'archive rejetée, `remote_library` ; passe 13 : règle anti-écho, réplique jamais nouvelle Variante (I50) |
| ADR-0041 | Port `ConnectedService` | accepté (passe 6) ; passe 7 : demande polymorphe, `upstream_snapshot` ; passe 10 : `instance_fingerprint`, identités amont stables à la réinstallation ; passe 13 : tickets d'une Unité purgée fermés, reprise en M1b des copies amont |
| ADR-0042 | Langues BCP 47 et contributeurs | accepté (passe 6) ; passe 7 : `normalize_lang()` versionnée, `text_version` référencé |
| ADR-0043 | Identités stables et normaliseurs versionnés | accepté (passe 7) ; passe 8 : identité des Variantes, `number_segment`, `external_id.id_scheme` ; passe 9 : `content_trust`, `derived_hash`, `number_segment_ordinal`, références sans secret, `remote_library` par serveur ; passe 10 : `remote_unit_id` = identité amont (`sourceId` + URL), `fetch_handle` en cache ; passe 11 : version de normaliseur citée par une pierre tombale jamais retirée, migrations d'identité qui consultent les pierres tombales |
| ADR-0044 | Restauration et époques | accepté (passe 7) ; passe 8 : réadoption par le miroir de provenance ; passe 9 : compteur d'époque `fs_intent.epoch`, génération à un cran, pierres tombales rejouées, dérive amont ; passe 10 : rejeu des pierres tombales sans suppression avant confirmation de l'admin ; passe 11 : ligne indéchiffrable inerte au rejeu ; passe 13 : blocage au rejeu limité aux `kid` du registre des `kid` émis |
| ADR-0045 | Progression par fichier | accepté (passe 7) ; passe 10 : forme temporelle `t=` réservée dans les locators ; passe 13 : signature `reading_position(carrier_type, carrier_id, …)`, `document_alias` sur `export_item` seulement |
| ADR-0046 | Miroir de provenance par racine physique | accepté (passe 8) ; passe 9 : format 1.1 : lignée par entrée de couverture, `unresolved`, entrée non fiable, pas de miroir sur `adopt_ro` ; passe 10 : format 1.2, décisions de suivi et âges classés (relus en suggestions) |
| ADR-0047 | Frontière de confiance du contenu | accepté (passe 9) ; passe 10 : original `untrusted` gardé en mode passage, tatouage heuristique sans dispense, mise à jour de sécurité du neutraliseur, attestation des plugins tiers ; passe 11 : attestation dès M2 pour tout plugin non first-party, destination `pull` servie depuis le dérivé |
| ADR-0048 | Effacement immédiat et pierres tombales | accepté (passe 9) ; passe 10 : portées `variant`/`place`/`series_binding`/`work`, journal authentifié, périmètre de purge ; passe 11 : valeurs brutes chiffrées (AEAD) au lieu d'empreintes salées et du HMAC, test d'appartenance sous chaque version de normaliseur citée, trousseau perdu ⇒ journal inerte ; passe 12 : `kid` cité jamais retiré, ligne au `kid` inconnu bloquante, `root_id` propre à la racine ; passe 13 : registre des `kid` émis, ligne au `kid` absent du registre inerte, `physical_root`, `tombstone rekey` après fuite, reprise en M1b des purges de M1a |
| ADR-0049 | Sessions, ré-authentification et appareils partagés | accepté (passe 10) ; passe 11 : axe crédential, actions réservées interdites aux clés et aux jetons, clés `admin` à expiration facultative (arbitrage porteur P4), sujet des clés, fraîcheur sans mot de passe local ; passe 13 : actions réservées déclenchées par un crédential, émetteurs système exemptés, sorties classées, pair hors de l'espace de PID refusé ; porteur 1 : sessions de 30 j par défaut, ré-authentification réduite (P2, P3), profil durci |
| ADR-0050 | Passkeys (WebAuthn) pour la connexion et la ré-authentification | accepté (porteur 1, P7, P8) ; M1b, EF-USR-025 |
| ADR-0051 | Thèmes du front : quatre directions sur un socle unique, « Bibliothèque » par défaut | accepté (porteur 2, Q2) ; M3 (mécanisme, A) / M4 (B, C, D), EF-UX-012 |
