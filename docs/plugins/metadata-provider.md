# Écrire un fournisseur de métadonnées (SDK 1.2)

Un fournisseur de métadonnées répond à la question « que sait-on de cette entité ? ». Il propose
des valeurs avec leur provenance ; l'hôte décide ce qu'il garde et ne remplace jamais un champ
verrouillé (EF-BIB-008). Le contrat vit dans `shelfhold.sdk.metadata` (licence MIT) et arrive avec
le **SDK 1.2** : c'est un ajout, un plugin écrit pour le SDK 1.1 continue de fonctionner (`api` N ou
N-1, voir l'ADR-0024). Un fournisseur déclare `min_sdk = "1.2"`.

La forme s'inspire du concept de source de métadonnées (identifier, couverture, correspondance
entre URL et identifiants) ; aucun code ni texte d'un projet tiers n'est repris.

## Le contrat

| Élément | Rôle |
| --- | --- |
| `MetadataManifest` | `key`, `version`, `api`, `min_sdk`, `terms_url`, `attribution`, `capabilities` (`IDENTIFY`, `COVER`, `RELEASE_CALENDAR`), `touched_fields`, `entity_types`, `quota`, `auth` |
| `EntityRef` | La cible : `entity_type` (`series`, `work`, `edition`, `volume`) et ses identifiants |
| `identify(f, target, hints)` | Retourne un `IdentifyResult` : des `FieldProposal` et des `RelationProposal` |
| `get_cover(f, target)` | Une `ImageRequest` ou `None` (si `COVER` est déclaré) |
| `url_for(ids)` / `ids_from_url(url)` | Correspondance identifiants et URL, aller-retour exact |
| `login` / `refresh` | Requis quand le manifeste déclare `auth` (flux d'EF-SRC-023) |

### Un ISBN désigne une Édition ou un Tome

Un ISBN identifie une publication, pas une Œuvre. `EntityRef(entity_type="work", ids={"isbn13": ...})`
est refusé à la construction. Pour une cible ISBN, les `FieldProposal` portent sur l'Édition (ou le
Tome) ; ce que le fournisseur sait de l'Œuvre (rattachement, auteur d'Œuvre) passe par une
`RelationProposal` (`work_relation`, `official_link`, `licensed_in`, `release`, `age_suggestion`).

### Provenance, confiance, quota

- Chaque proposition porte `Provenance(provider, fetched_at, source_url, licence, attribution)`.
  `provider` est la clé du plugin ; `source_url` ne contient aucun secret.
- `confidence` est comprise entre 0 et 1. Seuls les champs listés dans `touched_fields` sont proposés.
- `quota` déclare le seau à jetons (`requests_per_second`, `burst`) et un plafond journalier. L'hôte
  l'applique ; le plugin ne s'auto-limite pas. `requests_per_second_with_contact` déclare le palier
  plus haut qu'un fournisseur accorde à un agent qui donne un contact : l'hôte ne l'ouvre que si
  l'instance a configuré `metadata_contact`. Le seau est celui du fournisseur, partagé par tous ses
  `Fetcher`, et son solde survit au processus (`provider_quota`) ; robots.txt compte comme une requête.
- Un fournisseur injoignable (5xx, 429, réponse qui n'est pas celle qu'il documente) lève
  `ProviderUnavailable(reason)` : l'hôte prévient l'administrateur (`metadata.provider_down`, au plus
  une fois par heure) et ne change rien d'autre.
- L'hôte ouvre le `Fetcher` : agent identifiable (`Shelfhold/x.y (+dépôt[; contact])`, jamais remplaçable
  par le plugin), domaines déclarés, robots.txt, cache RFC 9111 obligatoire (jamais pour une requête
  authentifiée). La couverture est une `ImageRequest` : l'hôte la télécharge et la décode dans le
  worker (ENF-SEC-003), le plugin ne reçoit ni ne rend jamais d'octets d'image.
- Un fournisseur ne propose jamais d'âge comme champ : un âge est une `RelationProposal`
  `age_suggestion`, enregistrée comme suggestion et jamais appliquée. Il ne propose pas non plus
  `cover`, le champ de l'hôte. Une valeur verrouillée, ou saisie à la main, n'est jamais remplacée.
- Aucun secret ne passe par le plugin : les clés sont injectées par l'hôte (EF-SRC-035).

## Kit de conformité

```python
import asyncio
from shelfhold.sdk.metadata import EntityRef, MatchHints
from shelfhold.sdk.testing import FakeFetcher, run_metadata_conformance

report = asyncio.run(
    run_metadata_conformance(
        MyProvider(),
        FakeFetcher.from_cassette(cassette),  # cassette synthétique : aucun réseau
        targets=[EntityRef(entity_type="edition", ids={"isbn13": "9780306406157"})],
        hints=MatchHints(title="Un titre"),
    )
)
report.assert_ok()
```

Contrôles : `manifest`, `api_compatible`, `sdk_compatible`, `terms_url` (https), `quota`,
`capabilities_implemented`, `entity_types_declared`, `proposals_typed` (champ déclaré, type déclaré,
provenance au nom du plugin), `provenance_opaque`, `isbn_targets_edition`, `cover_opaque`,
`url_roundtrip`, `json_roundtrip` et `offline` (toute requête hors du script échoue). Fournissez au
moins une cible ISBN de type Édition. Le fournisseur factice `FakeMetadataProvider` sert de modèle.

## Liste de contrôle d'admission

La liste d'admission d'ENF-LEG-003 s'applique aux fournisseurs de métadonnées proposés dans l'index
officiel, comme aux sources. Elle figure dans le gabarit de PR
(`.github/pull_request_template.md`, section « Admission d'un plugin de métadonnées ») :

1. preuve d'autorisation ou licence libre de l'API et des données ;
2. conditions lues : `terms_url` au manifeste, robots lus et respectés ;
3. limites déclarées : `quota`, attribution et mention de la source dans la provenance ;
4. aucune clé ni secret dans le dépôt, `auth` déclaré au besoin ;
5. kit de conformité vert, hors réseau, sur fixtures synthétiques ;
6. revue CODEOWNERS.
