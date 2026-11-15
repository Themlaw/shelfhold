# e2e — parcours sur l'image

Les parcours (`tests/test_*.py`) montent la pile `compose.yaml` autour de l'image à tester ; ceux
qui passent par un navigateur (`tests/*.spec.ts`, config `playwright.image.config.ts`) sont lancés
par leur enveloppe pytest. La CI les distribue en shards (`shards.py`).

## Référence des écrans clés (EF-UX-012)

`tests/references/key-screens.json` est la référence **commitée** : pour chaque écran clé, chaque
thème et chaque schéma de couleurs, les jetons `--color-*` appliqués par le navigateur et la
structure de la page (repères et titres). `test_key_screens_snapshot_per_theme` la compare à chaque
exécution. Les PNG sont déposés en artefacts, jamais comparés (leur rendu dépend des polices de la
machine).

La référence ne se refait **jamais** automatiquement, et jamais en CI (AGENTS.md) : le test refuse
si `CI` est défini. Quand un écran ou un thème change à dessein, sur une machine avec Docker :

```sh
docker build -f docker/Dockerfile -t shelfhold:ci .
cd e2e && npm ci && npx playwright install chromium
SHELFHOLD_IMAGE=shelfhold:ci E2E_UPDATE_REFERENCE=1 \
  uv run --project ../backend pytest tests/test_m3_journey.py -k test_key_screens_snapshot_per_theme
git diff tests/references/key-screens.json   # relire chaque écart, puis le commiter avec sa raison
```

## Navigation fencing (ENF-PRIV-001)

Every Playwright context is fenced by `context.route('**/*')`: any request whose origin
differs from `baseURL` is aborted and counted in the array returned by
`refuseOutsideBaseUrl` (see `tests/fence.ts` and `workshop/fixtures.ts`). That count is
asserted to be empty at the end of every journey. `test_spa_requests_outside_base_url_are_refused_and_counted`
is the proof that a third-party image injected on purpose is refused and counted.
