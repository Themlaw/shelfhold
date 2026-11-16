# Le kit de conformité des destinations

Le kit des destinations est le chapitre « destination » du kit de conformité ([kit.md](kit.md)). Il est livré dans
le SDK (`shelfhold.sdk.testing`, licence MIT), ne dépend d'aucun code de l'hôte et tourne hors réseau, sans
toucher le disque : une destination **décrit** (plan `FsOp`, `ApiCall`), l'hôte exécute. Une destination n'est
dite « conforme » que si ce kit passe.

## Lancer le kit

```python
from shelfhold.sdk.fsops import ContentRef, FsOp, FsOpPlan
from shelfhold.sdk.testing import DestinationScenario, assert_destination_conforms
from shelfhold.sdk.testing.destination_kit import SYNTHETIC_ITEM

plan = FsOpPlan(
    ops=(
        FsOp(op="mkdir", rel_path="Series A"),
        FsOp(op="write", rel_path="Series A/Tome 1.cbz",
             content_ref=ContentRef(kind="export_item", ref=SYNTHETIC_ITEM)),
    )
)

def test_my_destination_conforms():
    assert_destination_conforms(MyDestination(), DestinationScenario(key="my-dest", plan=plan))
```

`DestinationScenario` dit ce que la destination a rendu pour l'item synthétique :

| Champ | Sens |
| --- | --- |
| `key` | la clé du plugin, qui fixe l'espace d'événements `plugin.<clé>.*` |
| `plan` | un `FsOpPlan`, ou le JSON (texte ou dict) rendu : un plan qui ne peut pas être construit est rapporté par son nom au lieu de lever |
| `calls` | des `ApiCall` supplémentaires (le corps d'un `push`, par exemple) |
| `events` | les types d'événements que la destination émet |

Le kit rejoue aussi, seul, `scan_calls` et `inventory_calls` de la destination. La fixture pytest
`check_destination` (`pytest_plugins = ["shelfhold.sdk.testing.pytest_plugin"]`) enveloppe
`assert_destination_conforms`. `run_destination_conformance` rend le rapport complet sans lever.

Le plan doit citer seulement `SYNTHETIC_ITEM`, planifié par le kit. `SYNTHETIC_WATERMARKED` est planifié **et tatoué** :
le citer échoue pour une destination `egress = externe`.

## Les contrôles

| Contrôle | Échoue quand |
| --- | --- |
| `capabilities_declared` | la destination n'a pas d'attribut `capabilities` |
| `transport_declared`, `egress_declared` | non déclaré, ou hors de `tree`/`dropbox`/`push`/`pull` et `foyer`/`externe` |
| `transport_push_requires_content_body` | `push` sans `content_body` : rien d'autre n'envoie d'octets par un appel |
| `access_partitioning_declared` | non déclaré, ou hors de `none`/`library`/`user` (une étagère < 18+ ne se lie qu'à `library` ou `user`) |
| `readback_declared`, `readback_has_a_way_to_read` | `readback` absent ou inconnu ; `verify` ou `fetch` sans `inventory_calls` qui décrive un appel |
| `can_purge_declared` | `can_purge` absent ou non booléen |
| `calls_roundtrip_json`, `calls_checked_before_run` | un appel ne se relit pas par `parse_api_call` ; un appel que l'hôte refuserait avant de l'exécuter : chemin interdit, contenu non planifié, fichier tatoué vers `externe`, **`DELETE` sans `can_purge`** |
| `plan_roundtrips_json`, `plan_checked_before_run` | le plan ne se relit pas à l'identique ; une opération que l'hôte refuserait juste avant de l'exécuter (`check_destination_plan_before_run`) |
| `plan_inside_root_outside_host_area` | une opération sort de la racine de la liaison (chemin absolu, `..`), écrit sous `.shelfhold/**`, ou est un `unlink` (réservé à l'hôte) |
| `no_forbidden_sidecar` | la destination déclare un sidecar qu'elle interdit (`forbidden_sidecars`), ou son plan en écrit un |
| `events_in_plugin_namespace` | un événement hors de `plugin.<clé>.*` (`job.failed`, l'espace d'un autre plugin) |
| `update_metadata_is_implemented` | `update_metadata` déclaré sans méthode `update_metadata` |
| `indexes_tree_needs_a_tree`, `indexes_tree_requires_grouping_and_normalizer`, `indexer_emits_no_fsop_write` | un indexeur qui n'a pas d'arbre, de regroupement ou de normaliseur, ou dont le plan écrit |
| `scenario_replays_something` | ni plan ni appel : un kit qui ne rejoue rien ne prouve rien |

## `can_purge` et `readback`

- `can_purge` est une **déclaration** que seul le chemin de purge de l'hôte lit, sous intention d'effacement. Un
  `DELETE` rendu par une méthode ordinaire de la destination est refusé sans `can_purge` ; la purge elle-même sort d'un
  appel que l'hôte demande (`purge --now`), jamais d'un appel libre.
- `readback` : `none`, une copie n'est jamais relue ; `verify`, elle sert à vérifier ; `fetch`, elle peut aussi servir
  de source d'octets. Seul `fetch` fait d'une copie une source.

## Ce que le kit ne prouve pas

Il ne remplace pas l'attestation de l'admin : `egress` et `access_partitioning` déclarés par un plugin tiers sont des
défauts proposés (EF-EXP-065). Il ne lance aucune requête réelle. Les secrets n'apparaissent jamais dans un message
d'échec, seulement le chemin et le motif.
