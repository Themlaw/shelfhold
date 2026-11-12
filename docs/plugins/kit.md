# Le kit de conformité (SDK 1.3)

Le kit est livré **dans le SDK** (`shelfhold.sdk.testing`, licence MIT) comme outil pytest. Il tourne hors
réseau (`record_mode = "none"`) sur des cassettes synthétiques : toute requête non scriptée échoue.

## Sources

```python
from shelfhold.sdk.testing import FakeFetcher, assert_conforms

report = assert_conforms(MySource(), FakeFetcher.from_cassette(cassette), reinstall=lambda: MySource())
```

`assert_conforms` lève `AssertionError` au premier échec ; `run_source_conformance` (asynchrone) rend le rapport
complet. La fixture `check_conformance` du plugin pytest (`pytest_plugins = ["shelfhold.sdk.testing.pytest_plugin"]`)
l'enveloppe. Les contrôles, par nom :

| Contrôle | Ce qu'il vérifie |
| --- | --- |
| `manifest`, `api_compatible` | le manifeste s'écrit et se relit ; `api` vaut N ou N-1 |
| `waiting_states_declare_exit` | chaque état d'attente propre déclare sa sortie (`retry`, `recheck`, `reconfigure`) et son événement `plugin.<clé>.<nom>` |
| `capabilities_implemented` | chaque capacité déclarée a sa méthode |
| `references_opaque` | aucun secret dans un `remote_id` ou un `source_key` |
| `unit_identity`, `json_roundtrip`, `list_units_idempotent` | identifiants d'Unités non vides, distincts, ordre distinct ; tout se sérialise ; deux appels donnent la même liste |
| `fetch_unit`, `server_queue`, `health_probe` | la livraison du contenu, et, pour un service connecté, la file côté serveur et la sonde |
| `simulated_reinstall` | **règle de stabilité** : à la réinstallation simulée, `SeriesRef`, `source_key` et `remote_unit_id` sont identiques ; un service connecté sans fabrique échoue |
| `offline`, `no_direct_io` | aucune requête hors cassette ; aucun socket, fichier ou processus ouvert par le plugin |

`fetch_range` est **optionnel** depuis le SDK 1.3 : la livraison d'une plage passe par le ticket du service
connecté (`request`, `poll`, `fetch`). Un plugin écrit pour le SDK 1.2 qui l'implémente continue de fonctionner.

## Fournisseurs de métadonnées

`run_metadata_conformance(provider, fetcher, targets=[...], hints=...)` : donnez au moins une cible ISBN (une
Édition) pour exercer la règle de typage de [metadata-provider.md](metadata-provider.md).

## Corpus d'EPUB piégés

`run_trapped_epub_corpus(neutralize_epub)` tient un neutraliseur contre le corpus : un piège à neutraliser
sort sans motif actif, un piège à refuser lève une exception.

## Destinations

**Il n'existe pas encore de kit pour les destinations : il arrive en M4**, avec la première destination
complète (contrôles de `readback`, de `can_purge` et des espaces de noms). D'ici là, la seule vérification
disponible est celle du plan d'opérations : `parse_destination_plan` et `check_destination_plan` de
`shelfhold.sdk.fsops` ([destination.md](destination.md)). Ne présentez pas un plugin de destination comme
« conforme » avant M4.

## Cassettes

Une cassette est un JSON synthétique : `{"interactions": [{"method", "url", "status", "body"}]}`. Aucun contenu
copié d'un site, aucune adresse réelle : utilisez des hôtes `.invalid`. Le test du gabarit ajoute un
**test d'attaque** : une source qui joint un hôte hors cassette doit faire échouer le kit.
