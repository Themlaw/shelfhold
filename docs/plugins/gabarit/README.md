# Gabarit de dépôt de plugin (source)

Une source minimale qui passe le kit de conformité, avec sa cassette synthétique.

```text
src/plugin_gabarit/source.py     la source : manifeste, recherche, série, Unités, fichier
tests/test_conformance.py          le test qui lance le kit, hors réseau
tests/cassettes/catalogue.json       les réponses scriptées (hôte réservé `.invalid`)
pyproject.toml, NOTICE-LICENCE.md
```

À changer : la clé (`KEY`), l'hôte (`HOST`, seul hôte joignable), le manifeste (`terms_url`, `rate_limit`,
`rights_basis`, attribution), l'analyse du catalogue. À garder : tout passe par le `Fetcher`, `remote_unit_id` vient de
l'amont, un format changé lève `SourceChanged`.

Lancer le test (depuis ce dossier, avec le SDK de Shelfhold importable) : `python -m pytest`. Le dépôt de
Shelfhold exécute ce même test.

Parcours complet : [../parcours.md](../parcours.md).
