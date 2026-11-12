---
status: accepté (amende ADR-0007)
date: 2026-11-02
amended: 2026-11-03 (passe 6) ; 2026-11-03 (passe 7) ; 2026-11-03 (passe 8) ; 2026-11-03 (passe 10) ; 2026-11-03 (passe 11) ; 2026-11-03 (passe 12)
decision-makers: mainteneur
---
# ADR-0024 — Contrat de données du SDK v1 et compatibilité `api` N-1

## Contexte et problème
ADR-0007 fixe un contrat de plugin unique avec isolation progressive, mais les types de données y restent esquissés. La revue d'évolutivité (OBJ-EVO-04 à -09, -13, -17, -18, -24, -25, -30 à -37) a montré que plusieurs cas réels n'étaient pas exprimables :
- une méta-source qui expose N sources (Suwayomi) ;
- des URL de pages qui expirent ;
- des images brouillées à reconstituer ;
- des flux d'authentification par appareil ou par captcha ;
- un changement d'identifiant de source ;
- un état de plugin qui évolue entre versions.

Une seule version cassante du contrat, publiée en M6, casserait tous les plugins tiers. Le SDK doit donc être complet dans sa *forme* avant d'être publié, même si V1 n'en utilise qu'une partie.

## Facteurs de décision
- « Concevoir large, livrer étroit » : types complets, implémentations minimales.
- Rester dans le monorepo en V1 pour pouvoir itérer sans casser de tiers.
- Une politique de compatibilité explicite avant la publication sur PyPI.
- Ne pas recopier de code GPL (formes de l'API Source de Calibre seulement).

## Options considérées
### A. Types complets dès M1 dans `shelfhold.sdk` (interne), publication `shelfhold-sdk` en M6, compatibilité `api` N-1 via un shim
- Avantages : la forme est testée par les plugins first-party pendant M1–M5 ; une seule rupture contrôlée ; les tiers ont une fenêtre de migration.
- Inconvénients : types « réservés » sans implémentation à maintenir ; le shim N-1 coûte du code.
### B. Publier le SDK dès M1, avec semver strict
- Avantages : communauté plus tôt.
- Inconvénients : les erreurs de forme deviennent des ruptures publiques ; la charge de support arrive avant la 1.0.
### C. Ne figer que ce que V1 utilise et étendre plus tard
- Avantages : moins de code.
- Inconvénients : ruptures répétées à chaque nouveau besoin (sous-sources, AuthFlow, désembrouillage).

## Décision
Option A.

**Types spécifiés dans `02-architecture.md` §4**
- `UnitInfo` (avec `remote_unit_id`, `number` texte, `part`, `track`, `source_order` et `revision_hint`).
- `SeriesRef` et `PageRef` paresseux (résolution à la demande, URL expirantes).
- `ImageRequest` (en-têtes, référent, `limit_key`).
- `Cursor` opaque, `FilterSpec`.
- `AuthFlow` (Basic, Form, ApiKey header|query|path, CookieImport, OAuth2 device code, Interactive/captcha manuel) et `SessionState{expires_at}` avec `refresh`.
- `DomainRule` (suffixes, jokers, `purpose`).
- Exceptions typées.

**Calendrier d'implémentation**

| Jalon | Contrats implémentés |
|---|---|
| V1 | `Source` et `ExportTarget` |
| M2 | `MetadataProvider`, sur la *forme* de l'API `Source` de Calibre : capabilities identify/cover, `touched_fields`, clé de pertinence, id↔URL ; pas le code GPL |
| Réservés, contrat esquissé | `Tracker`, `Importer`, `Notifier`, `Processor` (dont le hook « descramble » qui produit `original_sha256` + `derived_sha256`, EF-BIB-006) |

**Sous-sources et identité des sources**
- Sous-sources dynamiques : `sub_sources()`, table `sub_source`, limitation par `limit_key` de source logique amont (EF-SRC-021).
- Alias de source : `source_aliases`, `migrate_ref`, `id_scheme` (EF-SRC-022).

**Versionnement et compatibilité**
- Le manifest déclare `api` (contrat de données) et `rpc_protocol` (transport, M6) séparément.
- L'hôte accepte `api` N et N-1 via un shim.
- L'état persisté du plugin est versionné (`memo_version`, `payload_schema`), avec une migration fournie par le plugin.
- Durée de support de N-1 : 12 mois après la sortie de N, à confirmer (Q20).

**Distribution**
- SDK interne au monorepo en V1, sous `backend/src/shelfhold/sdk/`.
- Paquet PyPI `shelfhold-sdk` (MIT, ADR-0014) publié en M6.

## Conséquences
- Positives : sous-sources, désembrouillage, authentification par appareil et renommage de source deviennent exprimables sans rupture. Les plugins first-party valident la forme pendant cinq jalons.
- Négatives : il faut des tests de contrat pour les types réservés (sérialisation, compatibilité), un kit de conformité (ENF-QUAL-003) et un shim N-1 à maintenir.

## Risque principal
Une forme fausse découverte après M6. Mitigations : la publication n'a lieu qu'après plusieurs plugins first-party (Suwayomi, OPDS, Gutenberg, Wikisource, lncrawl, Kavita, OpenLibrary), et le shim N-1 donne une fenêtre de correction.

## Réversibilité
Haute avant M6 (code interne). Moyenne après, grâce à N-1.

## Amendement passe 4 (2026-11-03)
Parcours de l'auteur de plugin (D13, GAP-4-15) : documentation dès M3 (`docs/plugins/`), kit de conformité pour les sources **et** les destinations, licence sous l'exception provisoire ; en M6 : inscription à l'index, champs `deprecated`/`replaced_by`/`min_host`, `migrate_state(from_version)` obligatoire quand `payload_schema` change, retour arrière refusé si l'état persisté est plus récent, événement `plugin.incompatible` (EF-SRC-030). La garde des plugins tiers (installés désactivés) est datée M1b ; M1a ne charge aucun plugin tiers.

## Amendement passe 6 (2026-11-03)
F1, F3, F9, F10 (GAP-6-01, GAP-6-03, GAP-6-09, GAP-6-14, GAP-6-15, U2).
- **`LanguageTag`** : type du SDK pour tout champ `lang`, construit et validé par l'hôte (`normalize_lang()`, BCP 47 canonique) ; le plugin transmet la valeur brute, conservée en `lang_raw` (ADR-0042, ENF-I18N-005).
- **Source** : capacité facultative `ranged_content` (`fetch_range(edition_ref, first, last)` : un fichier pour une plage d'Unités) et champs `UnitInfo.volume_label`, `UnitInfo.volume_closed` (EF-SRC-032) ; opérations facultatives `request(unit) → ticket`, `poll(ticket)`, `fetch(ticket)`, `cancel(ticket)` pour l'acquisition en deux temps, les sources synchrones gardent `fetch(unit)`.
- **ConnectedService** : port au-dessus de `Source`, capacités déclarées (ADR-0041, EF-SRC-033).
- **ExportTarget** : `transport`, `egress`, `accepted_content_types` (EF-EXP-058).
- **MetadataProvider** (esquisse fixée dès M2, GAP-6-14) : `identify(target: EntityRef, hints)` ; résultats = `FieldProposal(entity_type, field, lang, value, confidence, provenance)` et `RelationProposal` ; `auth: AuthFlow` réutilisé d'EF-SRC-023 ; `rate_limit` dans le manifest.
- Tous ces ajouts sont **additifs** dans `api` v1 (champs facultatifs, capacités absentes par défaut).

## Amendement passe 7 (2026-11-03)
G5, G6, V4 (GAP-7-05, GAP-7-06).
- Cibles `UnitRef` et `RangeRef` ; `server_queue.request(target: UnitRef | RangeRef) → Ticket` ; `fetch_range(target: RangeRef)` en est le cas synchrone ; `RangedPayload.bounds` donne les bornes de chaque Unité quand la source les connaît (EF-SRC-034, EF-LEC-014).
- `SourceManifest.translation_nature` (surchargeable par Unité) : `published`, `community`, `machine`, `unknown` (EF-BIB-049).
- **Sérialisabilité** : tout type du SDK fait un aller-retour JSON (`test_sdk_models_json_roundtrip`, M1a), en vue du bac à sable JSON-RPC de M6 ; aucune signature ne prend ni ne rend d'objet vivant hors `Fetcher` (EF-SRC-001).

## Amendement passe 8 (2026-11-03)
H3 (GAP-8-12, GAP-8-13).
- **Opérations fichier des destinations** : l'objet `f` passé aux destinations (EF-EXP-001) n'était pas dans l'exception « seul objet vivant : `Fetcher` » de la passe 7 (V4). Il devient un **plan d'opérations sérialisable** (`list[FsOp]`, chemins relatifs à la racine de la liaison, contenu désigné par référence) que l'hôte valide et exécute ; l'autre option (déclarer `FsOps` second mandataire RPC) est écartée, car elle garde un objet vivant à travers la frontière du bac à sable de M6. Forme fixée avant la documentation des auteurs (M3), testée par aller-retour JSON dans le kit destinations (M4).
- **Clé de plugin et signataire (M6)** : `(key, index, clé publique du signataire)` est épinglé à l'installation ; un changement de signataire est une nouvelle installation, jamais une mise à jour ; état, réglages et secrets transférés seulement après confirmation (EF-SRC-017).

## Amendement passe 9 (2026-11-03)
J1, J5 (X1, GAP-9-01, GAP-9-09).
- `SourceManifest.content_trust: publisher | untrusted`, défaut `untrusted`, fait partie du contrat v1 **dès M1a** ; un plugin qui ne le déclare pas est `untrusted` (ADR-0047).
- Les références rendues par une source (`remote_id`, `SeriesRef`, `FileAsset.href`, `RangeRef`) sont **opaques, sans secret et stables à la rotation des identifiants** ; le Fetcher refuse ou réécrit (gabarit `{secret}`) toute référence qui contient un secret connu ou une partie `userinfo` ; règle documentée pour les auteurs avant M2 (I38).
- Les plans `FsOp` des destinations ne peuvent viser aucun chemin réservé `.shelfhold` (I37) ; validation côté hôte, qui ne casse aucun plugin légitime.

## Amendement passe 10 (2026-11-03)
K3, K9 (GAP-10-03, GAP-10-08, GAP-10-10).
- **Stabilité des identités** : `remote_unit_id` et `SeriesRef` **doivent** rester stables à la réinstallation de l'outil connecté (base recréée, renumérotation), et non seulement à la rotation des secrets ; jamais l'identifiant de la base de l'outil (EF-SRC-036). Le kit de conformité joue une réinstallation simulée. Règle écrite avant la doc auteur (M3), pour qu'aucun plugin tiers n'ait à migrer son état.
- **Valeurs déclarées par un plugin tiers** (`content_trust: publisher`, `access_partitioning`) : défauts proposés ; l'hôte ne les retient que pour un plugin first-party admis ou sur attestation de l'admin par instance.
- **Contrat de destination** : `unlink` réservé à l'hôte, `can_purge`, `readback`, `render()`, `write_inline`, espaces de noms `plugin.<clé>.*` (ADR-0011, EF-EXP-065).

## Amendement passe 11 (2026-11-03)
L1, L5, L6 (GAP-11-09, GAP-11-11, Z3).
- **Âge** : le type du SDK et la colonne sont un **âge minimal entier** plus un drapeau `age_x` ; les sources ne renvoient que `content_warning` (neutre vis-à-vis de la langue), aucun contrat de plugin n'est touché.
- **États d'attente propres** (`plugin.<clé>.*`, M3) : le manifeste déclare pour chacun une action de sortie générique (`retry`, `recheck`, `reconfigure`), vérifiée par le kit (`test_plugin_waiting_state_declares_exit`).
- **Attestation** de `publisher` et de `content_trust` : appliquée à **tout plugin non first-party dès M2**, isolé ou non (`test_non_first_party_publisher_needs_attestation_from_m2`).
- **Identités** : `migrate_ref`, alias et `replaced_by` passent l'alias inverse au test d'appartenance des pierres tombales (ADR-0048).

## Exigences liées
EF-SRC-001, EF-SRC-002, EF-SRC-003, EF-SRC-016, EF-SRC-017, EF-SRC-020, EF-SRC-021, EF-SRC-022, EF-SRC-023, EF-BIB-006, EF-BIB-013, EF-BIB-024, EF-EXP-001, ENF-QUAL-003, ENF-LEG-005, EF-SRC-030, EF-SRC-019, EF-SRC-032, EF-SRC-033, EF-EXP-058, ENF-I18N-005, EF-SRC-034, EF-BIB-049, EF-EXP-001, EF-SRC-017, EF-BIB-051, EF-SRC-035, EF-SRC-036, EF-EXP-065, EF-INT-023, EF-EXP-011, EF-BIB-054.

## Sources
- https://github.com/kovidgoyal/calibre/blob/master/src/calibre/ebooks/metadata/sources/base.py (forme de `Source`, GPL-3.0, non copiée)
- https://github.com/Suwayomi/Suwayomi-Server (méta-source exposant N sources)
- https://github.com/mihonapp/mihon (contrats `Source`, `HttpSource`, pages paresseuses)
- https://packaging.python.org/en/latest/specifications/entry-points/
- https://semver.org/lang/fr/
- `deliverables/review/contradicteur-evolutivite.md` (OBJ-EVO-04→09, -13, -17, -18, -24, -25, -30→37), ARBITRAGES A7–A9, `research/08-closure-A.md` §6.2
- `deliverables/review/chasseur-trous-passe4.md` (GAP-4-15)
- `deliverables/review/ARBITRAGES-passe4.md`
- `deliverables/review/chasseur-trous-passe7.md`, `deliverables/review/explorateur-passe7.md`, `deliverables/review/ARBITRAGES-passe7.md` (passe 7)
- `deliverables/review/chasseur-trous-passe8.md`, `deliverables/review/explorateur-passe8.md`, `deliverables/review/ARBITRAGES-passe8.md` (passe 8)
- `deliverables/review/chasseur-trous-passe9.md`, `deliverables/review/explorateur-passe9.md`, `deliverables/review/ARBITRAGES-passe9.md` (passe 9)
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)

## Amendement passe 12 (2026-11-03)
M10 (GAP-12-13, GAP-12-11).

- Contrat de destination : capacité **facultative** `update_metadata` (champs acceptés, verrou), additive, déclarée avant le kit destinations de M4 ; une destination déjà écrite sans elle reste valide (repli : proposition de réécriture avec l'avertissement d'EF-EXP-037). Aucun plugin existant n'est cassé.
- Une Édition `unshelved` n'est jamais confiée à une destination.

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)

## Amendement passe 13 (2026-11-03)
N6 (AB1).

- `ApiCall.body` est un champ **facultatif** (`None` par défaut, `json` conservé) : ajout compatible du contrat `api` v1, sans changement cassant pour les plugins ni les destinations déjà écrits ; le kit refuse `transport = push` jusqu'à M4, pour qu'aucun plugin ne déclare un transport que le cœur ne sait pas exécuter.
- FanFicFare/AO3 : plugin **tiers** seulement, jamais first-party ni dans le dépôt (position de l'éditeur d'AO3 contre le moissonnage, œuvres restreintes derrière connexion) ; aucun changement de contrat.
- Test : `test_kit_refuses_push_transport_before_m4`.

Source : `deliverables/review/chasseur-trous-passe13.md`, `deliverables/review/explorateur-passe13.md`, `deliverables/review/ARBITRAGES-passe13.md` (passe 13)

## Amendement SDK 1.3 (M3 L34, gel avant la documentation des auteurs)
Ajouts compatibles (un plugin écrit pour 1.2 passe le kit 1.3) :
- Le plan `FsOp` est sérialisable en JSON (`FsOpPlan`) et enraciné ; tout chemin réservé (`.shelfhold`, `.shelfhold-*`, sous toute casse, forme Unicode, séparateur, point ou espace final) est refusé ; `unlink` reste à l'hôte et n'entre jamais dans un plan de destination. L'hôte revalide le plan juste avant de l'exécuter.
- Les événements d'un plugin vivent sous `plugin.<clé>.<nom>`, la clé étant celle sous laquelle l'hôte a chargé le plugin.
- `PluginManifest.waiting_states` : chaque état d'attente propre déclare une sortie (`retry`, `recheck`, `reconfigure`) ; le kit le vérifie (`waiting_states_declare_exit`).
- D10 : `Source.fetch_range` devient facultatif (défaut `NotImplementedError`) ; le kit ne l'appelle plus, l'adaptateur lncrawl ne l'implémente plus (le ticket reste le chemin de production).
