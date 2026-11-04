# AGENTS.md — consignes pour les agents de code

Ce dépôt est développé majoritairement par des agents orchestrés par le mainteneur. Politique : [AI_POLICY.md](AI_POLICY.md). Contribution : [CONTRIBUTING.md](CONTRIBUTING.md).

## Règles

- **TDD** : écrire le test qui échoue, **montrer l'échec**, puis implémenter. Marqueur `@pytest.mark.req("EF-…")` sur chaque test lié à une exigence.
- Ne jamais modifier ou supprimer un test pour le faire passer sans le signaler explicitement.
- Ne jamais régénérer snapshots ou cassettes sans demande explicite ; jamais en CI.
- Respecter les frontières de modules (import-linter) ; pas de nouvelle dépendance sans justification dans la PR.
- Fixtures synthétiques ou du domaine public uniquement. N'écrire aucun nom de site non autorisé ni lien vers du contenu non autorisé, ni code ou documentation touchant à la levée de protections techniques.
- Chaque diff est relu par un autre agent avant fusion ; la CI (`ci-success`) doit être verte ; le mainteneur relit à chaque jalon.
- Commits : Conventional Commits, signés DCO (`git commit -s`). Ne jamais pousser sur `main` ; toujours une PR.
- Aucune adresse e-mail personnelle dans les fichiers ni dans les commits (noreply uniquement).
- Shelfhold n'embarque aucun modèle d'IA.

## Commandes

Les tâches sont définies avec `mise` (`mise.toml`, à venir en M0) : `setup`, `dev`, `test`, `test:watch`, `lint`, `fmt`, `e2e`, `docs`, `build-image`, `cassettes:record`, `bench`.

## Références

Spécification : [`docs/spec/`](docs/spec/README.md). Décisions : [`docs/adr/`](docs/adr/README.md).
