# Écrire une source (SDK 1.2)

Une **source** donne à Shelfhold des séries et des fichiers : elle cherche, décrit, liste les Unités et fournit leur
contenu. Le contrat vit dans `shelfhold.sdk.source` (licence MIT) et s'appuie sur le **SDK 1.2** (`SDK_VERSION`).
Un fournisseur de métadonnées, qui ne fournit pas de fichier, est un autre plugin : voir
[metadata-provider.md](metadata-provider.md). Un plugin tiers est désactivé par défaut et tourne avec les droits de
Shelfhold : lisez [../admin/plugins-tiers.md](../admin/plugins-tiers.md) avant d'en publier un.

## Le manifeste

`SourceManifest` (clé, version, `api`, nom, `permissions`, `settings_schema`, `terms_url`, `respects_robots`,
`attribution`, `langs`, `content_kinds`, `url_patterns`, `capabilities`, `rate_limit`, `auth`, `id_scheme`,
`rights_basis`, `rights_jurisdiction`, `content_trust`, `tests`). Points qui comptent :

| Champ | Ce que l'hôte en fait |
|---|---|
| `permissions.domains` | Les seuls hôtes joignables : motifs de domaine, avec `base_url_setting` quand l'adresse est un réglage de l'admin. Toute autre requête est refusée avant de partir. |
| `terms_url` | Adresse https des conditions sous lesquelles la source est admise (obligatoire pour l'index officiel). |
| `rate_limit` | Débit (`requests_per_second`) et concurrence : l'hôte les applique, le plugin ne s'auto-limite pas. |
| `rights_basis` | `us_public_domain`, `worldwide_public_domain`, `licence` ou `user_supplied`. Pour les États-Unis, écrivez « libre de droits aux États-Unis », jamais « source légale ». |
| `content_trust` | `untrusted` par défaut : l'hôte neutralise les EPUB avant tout export. `publisher` n'est qu'un défaut proposé, que l'admin doit attester ([../admin/neutraliseur.md](../admin/neutraliseur.md)). |
| `settings_schema` | Schéma plat ; un champ `writeOnly` ou `format: password` est un secret, injecté par l'hôte et jamais relu. |
| `capabilities` | `SEARCH`, `LATEST`, `POPULAR`, `URL_RESOLVE`, `LOGIN`, `FILTERS`, `METADATA_ONLY`, `FOLLOW`, `SUB_SOURCES`, `RANGED_CONTENT` ; un service connecté déclare aussi sa file côté serveur. |

## Les méthodes

Le protocole `Source` : `search`, `resolve_url` (si `URL_RESOLVE`), `get_series`, `list_units`, `fetch_unit`,
`fetch_range` (si `RANGED_CONTENT`), `resolve_page`, `login` et `refresh` (si `auth`), `migrate_ref`, `migrate_memo`.
Chaque méthode reçoit un `Fetcher` fourni par l'hôte : agent identifiable, cache, robots, domaines déclarés. Une source
n'ouvre jamais elle-même un socket, un fichier ou un processus : un détecteur d'entrées-sorties directes le refuse et
fait échouer la conformité.

Les erreurs sont typées (`AuthRequired`, `RateLimited`, `ContentRemoved`, `SourceChanged`, `TemporarilyUnavailable`,
`NotConfigured`) : leur code pilote l'état de santé. Un changement de format de la source est `SourceChanged`, jamais
un contenu vide.

## Kit de conformité

```python
import asyncio
from shelfhold.sdk.testing import FakeFetcher, run_source_conformance

report = asyncio.run(run_source_conformance(MySource(), FakeFetcher.from_cassette(cassette)))
report.assert_ok()
```

La cassette est synthétique : aucun réseau n'est possible en test, et aucun dépôt d'extensions n'est contacté.
`run_trapped_epub_corpus` rejoue le corpus d'EPUB piégés contre le neutraliseur. Les tests de la CI du dépôt n'utilisent
que des fichiers synthétiques ou du domaine public.

## Liste d'admission (ENF-LEG-003)

Une source proposée dans l'index officiel passe cette liste ; le mainteneur la valide, par revue CODEOWNERS, avant le tag.
Elle figure dans le gabarit de PR (`.github/pull_request_template.md`, section « Admission d'une source »).

1. **Preuve d'autorisation ou licence libre** : le texte de la licence ou l'autorisation écrite du site, de l'API ou de
   la bibliothèque, conservée avec la PR.
2. **Conditions et robots lus** : `terms_url` au manifeste, `robots.txt` lu et respecté (`respects_robots`).
3. **Limites déclarées** : `rate_limit`, `permissions.domains` au plus juste, attribution quand la licence l'exige.
4. **Aucun secret dans le dépôt**, aucune adresse personnelle, aucun nom de site non autorisé.
5. **Kit de conformité vert**, hors réseau, sur des cassettes synthétiques.
6. **Revue CODEOWNERS.**

Une source ne se présente jamais comme un moyen de joindre un site qui ne répond pas chez vous : le proxy par source
ne sert qu'au routage réseau du foyer.

### Les sept sources livrées avec M2

| Source | `terms_url` | Base de droits | Preuve à valider |
|---|---|---|---|
| `gutenberg` | `https://www.gutenberg.org/policy/terms_of_use.html` | libre de droits aux États-Unis | catalogue officiel et miroir de la liste officielle, jamais le site |
| `wikisource` | `https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use` | licence CC BY-SA | outil d'export du wiki, attribution construite |
| `standardebooks` | `https://standardebooks.org/about` | libre de droits aux États-Unis | une adresse par livre ; flux OPDS réservé aux patrons |
| `openlibrary` (métadonnées) | `https://openlibrary.org/developers/api` | données CC0 | API documentée, agent identifiable |
| `opds` | `docs/admin/sources.md` du dépôt | fournis par l'utilisateur | catalogue de l'utilisateur, anti-écho |
| `import_folder` | `docs/admin/dossier-import.md` du dépôt | fournis par l'utilisateur | aucun réseau |
| `lncrawl` | licence du logiciel lncrawl | selon l'instance de l'utilisateur | instance de l'utilisateur, aucun crawler fourni |

Le mainteneur valide cette liste **une fois**, sur le récapitulatif de la release (ADR-0035, D18), et non PR par PR.
Au moment de la rédaction de cette page, cette validation n'a pas encore eu lieu.
