# Contribuer à Shelfhold

Merci de votre intérêt. Le projet est en pré-alpha (M0) : lisez d'abord [ROADMAP.md](ROADMAP.md), [AI_POLICY.md](AI_POLICY.md) et [GOVERNANCE.md](GOVERNANCE.md). Le Code de conduite ([CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)) s'applique partout.

## TDD : le test rouge d'abord

1. Écrivez le test qui exprime le critère de l'exigence (`EF-…` / `ENF-…`) et **voyez-le échouer pour la bonne raison**.
2. Commit rouge séparé quand c'est praticable (`test: … (red)`).
3. Écrivez le code minimal qui passe (vert), puis refactorez sans toucher aux tests.
4. Ne modifiez jamais un test pour le faire passer sans le dire explicitement dans la PR. Ne régénérez jamais snapshots ou cassettes en CI.

Règles complètes : [`docs/spec/03-roadmap-tdd.md`](docs/spec/03-roadmap-tdd.md) §4. Couverture diff >= 85 %, globale >= 80 %.

## Conventional Commits

`feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`, `ci:`… Les releases sont générées à partir de ces messages. Les PR sont fusionnées en squash, historique linéaire.

## DCO 1.1 (pas de CLA)

Chaque commit porte une ligne `Signed-off-by:` ([Developer Certificate of Origin 1.1](https://developercertificate.org/)) :

```
git commit -s
```

Le contrôle `dco` (job de `ci.yml`) bloque toute PR dont un commit n'est pas signé, y compris ceux du mainteneur. Il n'y a **pas de CLA** et aucune cession de droits.

## Clause entrante

Toute contribution est reçue sous **AGPL-3.0-or-later avec l'exception de plugin de [LICENSE](LICENSE)** (y compris dans la version relue de cette exception), et **sous MIT pour `sdk/`**. Aucun code ne passe du cœur AGPL au SDK MIT sans l'accord de ses auteurs : un changement de `sdk/` qui recopie du code du cœur est refusé en revue. Chaque fichier de `sdk/` porte `SPDX-License-Identifier: MIT`.

## E-mail de commit

Utilisez de préférence l'adresse **noreply** de GitHub (`<id>+<pseudo>@users.noreply.github.com`) : l'historique est public et définitif.

## Migrations Alembic

Une migration publiée ne change jamais. Une PR qui en ajoute une la fige elle-même, avec la prochaine mineure :
`python3 tools/check_patch_migrations.py --freeze 1.1` (remplacer `1.1` par la mineure qui la livrera), puis commiter
`tools/published-migrations.toml`. La CI de PR lance `python3 tools/check_patch_migrations.py` et échoue sinon (`docs/releasing.md`).

## Fixtures synthétiques uniquement

Tests, fixtures et cassettes sont **synthétiques ou du domaine public**. Jamais de contenu protégé, jamais de capture d'un site tiers. Ne citez dans le code, la documentation, les issues ou les PR **aucun nom de site non autorisé** ni lien vers du contenu non autorisé : ces éléments sont supprimés.

## Écrire une source

Une source passe le kit de conformité (`shelfhold.sdk.testing`), déclare ses conditions (`terms_url`, robots, limites) et passe par la **liste d'admission** : preuve d'autorisation ou licence libre. Toute PR visant une source non autorisée est fermée (voir [ADR-0015](docs/adr/0015-posture-legale.md)).

## Tests découpés en shards

- La CI lance `backend-unit` en matrice (hash du nom de chaque test), `backend-integration` (aussi en matrice), puis `coverage` qui combine les `.coverage.*` et applique les seuils (80 % global, 85 % sur le diff).
- Un shard en local : `cd backend && uv run pytest tests/unit -n auto --shard 2/4 --no-cov` (intégration : `tests/integration`, 8 shards).
- Augmenter N : changer `BACKEND_SHARDS` (ou `BACKEND_INTEGRATION_SHARDS` pour l'intégration, `E2E_SHARDS` pour Playwright) dans le job `changes` de `.github/workflows/ci.yml`, seul endroit à toucher.
- Playwright en local : `cd e2e && npx playwright test --shard=1/2`.
- Un nouveau test va dans `tests/unit` (rapide, sans E/S lourde) ou `tests/integration` (base, application, sous-processus).

### Quelle CI pour quels fichiers

Sur une **PR**, chaque job ne tourne que si ce qu'il teste a pu changer (job `changes`, étape `scope`). Sur un **push vers `main`**, tout tourne. `lint`, `denylist`, `traceability`, `dco` et le titre tournent toujours ; `ci-success` accepte un job sauté seulement quand le périmètre dit qu'il n'avait pas à tourner.

| Fichier changé | Jobs lancés (en plus des toujours-actifs) |
| --- | --- |
| `backend/src/**`, `backend/pyproject.toml`, `backend/uv.lock`, `backend/tests/conftest.py`, modules d'aide à la racine de `backend/tests/`, `backend/tests/fixtures/**` (et `fakes`, `support`, `cassettes`), `docker/**`, `frontend/**`, `open-api/**`, `ci.yml` | tout : `backend-unit`, `backend-integration`, `backend-suites` (property, golden, faults), `backend-bench`, `coverage` (+ diff 85 %), `fixtures` ; `frontend`, `openapi`, `client-drift`, e2e front, image + e2e image selon les dossiers touchés |
| `backend/tests/unit/**` | `backend-unit`, `fixtures` |
| `backend/tests/integration/**` | `backend-integration` |
| `backend/tests/golden/**`, `faults/**`, `property/**` | `backend-suites`, uniquement la suite touchée |
| `backend/tests/bench/**` | `backend-bench` |
| `compose*.yaml`, `.env.example`, `.dockerignore` | `backend-unit` (tests statiques du compose), build de l'image et le seul scénario `smoke-install` |
| `.github/workflows/**` autres que `ci.yml` | `backend-unit` (tests du workflow) ; l'image et tous ses scénarios si `image*.yml` |
| `e2e/**` | e2e front, image et tous ses scénarios, `reader-bench` (rapport seul, jamais bloquant) |
| `*.md`, `docs/**` seuls | aucun test lourd |

`coverage` n'a lieu que lorsque le code de production change : il combine les données de toutes les suites, et une combinaison partielle échouerait le seuil global. Une PR de tests seuls n'a donc pas de rapport de couverture. La règle est figée par `backend/tests/unit/test_ci_workflows.py`.

## Politique de support

Support « au mieux », sans garantie, par un mainteneur bénévole.

- Discussions : questions et aide.
- Issues : bugs reproductibles uniquement (version, module, logs **sans URL ni jeton**).
- `wontfix:scope` : demandes hors périmètre (voir la liste « Jamais » du cahier des charges).
- Une issue sans reproduction est fermée après 60 jours.

## Code assisté par IA

Voir [AI_POLICY.md](AI_POLICY.md). Les contributeurs extérieurs restent responsables de ce qu'ils soumettent : pas de PR autonome d'agent.
