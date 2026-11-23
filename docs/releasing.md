# Publier une version

Une release est une action manuelle du mainteneur, en quatre étapes. Aucun jeton n'a le droit de créer une PR.

1. **Préparer la PR de version** (titre `chore(release): X.Y.Z`) :
   - `CHANGELOG.md` : nouvelle section `## X.Y.Z - AAAA-MM-JJ` avec les changements, et la ligne
     `Migration : oui|non, réversible : oui|non` (ENF-OPS-002) ;
   - `tools/published-migrations.toml` : toute migration est déjà figée ; la CI de PR (étape du job `lint`, sans `--tag`) échoue sinon.
     Une PR qui ajoute une migration la fige elle-même : `python3 tools/check_patch_migrations.py --freeze 1.1`, où `1.1` est la
     prochaine mineure (la ligne qui la livrera) ; une release `X.Y.Z` avec Z > 0 refuse toute migration dont `since` dépasse `X.Y` ;
   - la version dans `backend/pyproject.toml` et `backend/src/shelfhold/__init__.py` (`__version__`) ;
   - le champ `info.version` de `open-api/openapi.json` (sinon le job `openapi` échoue).
2. **Fusionner la PR** : `ci-success` doit être vert sur le commit fusionné, l'image `sha-<commit>` est alors publiée sur GHCR.
3. **Benchmarks de release** (ENF-PERF-006, [03 §4](spec/03-roadmap-tdd.md) ; le détail : [governance/benchmarks.md](governance/benchmarks.md)) :
   sur le lanceur qui mesure les releases, `cd e2e && npm run test:bench` puis
   `python3 bench/compare.py test-results/bench_reader_page_turn.json` : la porte échoue si le p95 du tour de page
   dépasse 150 ms ou régresse de plus de 20 % du dernier résultat enregistré (`e2e/bench/reference.json`) — la
   régression bloque la release. Une amélioration, ou un environnement de mesure changé, se réenregistre à dessein
   dans la PR de release ; la référence ne se régénère jamais automatiquement. Les benchmarks ne tournent jamais en nightly.
4. **Créer la release** (le tag `vX.Y.Z` est créé sur `main`) :
   ```sh
   gh release create vX.Y.Z --target main --title vX.Y.Z --notes-file <(extrait de la section du CHANGELOG)
   ```
   ou `--notes-from-tag` avec un tag annoté porteur des notes.
5. **Le workflow `release.yml`** se déclenche sur `release: published` et, dans un seul job `packages: write` :
   vérifie que le tag est `vX.Y.Z`, résout son commit, attend (borné) un `ci-success` vert sur ce commit, puis ajoute les tags
   `X.Y.Z`, `X.Y` et `latest` au **digest déjà publié** (`sha-<commit>`) : jamais de reconstruction.
   `X.Y.Z` est immuable (refus s'il existe avec un autre digest) ; `X.Y` et `latest` ne reculent jamais : un correctif d'une
   ancienne mineure, publié après une version plus récente, ne les déplace pas. `latest` n'est posé que par une release finale
   (décision du porteur du 15/11, [docs/decisions/M4.md](decisions/M4.md)).
6. **Assets de release** (EF-INT-027) : un second job de `release.yml` (`schema-assets`, `contents: write`) génère `schema.sql` et
   `data-dictionary.md` avec `.github/scripts/release-assets.sh` et les attache à la release (`gh release upload --clobber`).
   `schema.sql` vient des migrations Alembic appliquées à une base vide ; le dictionnaire ajoute une description par table et par colonne
   (`backend/src/shelfhold/docgen/schema_descriptions.toml`) : un test échoue si l'une manque. Ni l'un ni l'autre ne s'écrit à la main.
7. **Preuves de release** (ENF-SEC-007, ENF-QUAL-007) : `release.yml` exécute la suite unitaire du tag (`test-report`, sans jeton d'écriture),
   puis `release-proofs` (`contents: write`) vérifie les attestations des digests publiés et attache `sbom.spdx.json` (amd64),
   `sbom-arm64.spdx.json`, `provenance.intoto.jsonl`, `test-report.xml` et `bench-reference.json` (la référence versionnée dans
   `.github/bench-reference.json`, car les artefacts expirent). Vérification à la main : `gh attestation verify oci://ghcr.io/themlaw/shelfhold@<digest> --repo Themlaw/shelfhold`.
8. **Signatures et second registre** (ENF-SEC-007, ENF-GOV-004) : les images sont signées par cosign à la publication sur `main`
   (`image-publish.yml`), pas par `release.yml`. Le job `second-registry` de `release.yml` ajoute les tags de version (et `latest`, quand il suit GHCR) sur le second registre
   si `CODEBERG_TOKEN` existe et si le digest y est déjà signé, sinon il l'indique dans son résumé et réussit. Vérification, réglages
   et limites : [docs/admin/verifier-les-images.md](admin/verifier-les-images.md).
9. **Site de documentation** (D17) : `docs.yml` construit le site du tag avec Zensical (mode strict) et le publie sur GitHub Pages.
   Activer Pages (source : GitHub Actions) est un réglage du dépôt, à faire une fois par le propriétaire.

**Reprise** : si le job a échoué (CI encore rouge, image absente, erreur du registre), relancer le workflow `release` depuis l'onglet
Actions (`workflow_dispatch`, entrée `tag` = `vX.Y.Z`). Le script est idempotent.

## Candidate de version (`vX.Y.Z-rc.N`)

Une rc est une **pré-version GitHub** : `gh release create vX.Y.Z-rc.N --target main --prerelease --title vX.Y.Z-rc.N --notes ...`.
Sans `--prerelease`, elle deviendrait la « Latest release » ; le workflow `release` refuse l'incohérence entre le drapeau et le tag.

- **Aucun tag d'image** : ni `X.Y.Z-rc.N`, ni `X.Y`, ni `latest` (jamais d'écriture pour une rc). `release.yml` attend le `ci-success` vert, vérifie la signature
  cosign du digest de `sha-<commit>`, puis s'arrête sans rien écrire dans le registre. `X.Y` ne peut donc pas avancer vers une rc.
- Les preuves sont jointes comme pour une version : SBOM, provenance (lues sur l'image `sha-<commit>`, attestations vérifiées),
  rapport de tests, `schema.sql` et dictionnaire.
- Sautés sans échec : le second registre (`second-registry`) et la documentation (`docs.yml` ne construit ni ne publie une rc).
- **Installer la rc** avec l'image du commit, déjà signée et attestée (`<commit>` : les 40 caractères du commit du tag) :
  ```sh
  digest="$(docker buildx imagetools inspect ghcr.io/themlaw/shelfhold:sha-<commit> --format '{{.Manifest.Digest}}')"
  cosign verify "ghcr.io/themlaw/shelfhold@${digest}" \
    --certificate-identity-regexp '^https://github\.com/Themlaw/shelfhold/\.github/workflows/image-publish\.yml@refs/heads/main$' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com
  ```
  puis fixer `ghcr.io/themlaw/shelfhold@${digest}` (ou `:sha-<commit>`) comme image dans `compose.yaml`, à la place de `:1.0`
  qui n'existe qu'à partir de `v1.0.0`.

## Publier le SDK (`sdk-vX.Y.Z`)

Le SDK `shelfhold-sdk` est un composant séparé de l'application ([ADR-0056](adr/0056-plugins-tiers-isoles-index-signe.md),
D10 et D24) : l'application sort en `v1.3.0`, le SDK en `2.0.0`. Sa publication ([D-P2](adr/0056-plugins-tiers-isoles-index-signe.md),
ENF-SEC-007) est **de confiance** : le jeton OIDC du job `publish` de `publish-sdk.yml` est la seule
crédential, **aucun jeton longue durée** — aucun `secrets.*` n'est référencé, aucun jeton d'API n'est créé — et l'action
officielle `pypa/gh-action-pypi-publish`, épinglée par SHA, signe les **attestations PEP 740** (la page du projet
sur PyPI les montre, une par fichier publié). Le job `build`, sans aucune permission, construit roue et sdist et les remet telles quelles :
ce qui est publié n'est jamais reconstruit. Les environnements protégés (`pypi`, `testpypi`) arrêtent la publication
jusqu'à la relecture du porteur.

Chaque PR fait déjà tourner le repli : le job `sdk-dist` de `ci.yml` construit roue et sdist, contrôle les métadonnées
**sans envoi**, installe la roue dans un environnement **sans l'hôte** et y joue le kit de conformité sur le gabarit
d'auteur (`docs/plugins/gabarit`) — et sur le plugin d'exemple dès qu'il existe (L10, convention `examples/<nom>/`).

**Étapes du porteur** (aucune n'est faite par le dépôt ; sans elles, ENF-SEC-007 reste **partielle** pour M6, le repli
`sdk-dist` tenant lieu) :

1. **Comptes** : réserver le projet `shelfhold-sdk` sur TestPyPI, puis sur PyPI (la première publication se fait
   toujours sur TestPyPI).
2. **Éditeur de confiance** : sur chaque projet, déclarer un éditeur de confiance (*trusted publisher*) — le dépôt, le
   workflow `.github/workflows/publish-sdk.yml`, et l'environnement (`testpypi` pour TestPyPI, `pypi` pour PyPI).
   **Jamais de jeton d'API** : aucun `PYPI_API_TOKEN` à créer ni à stocker.
3. **Environnements protégés** : dans les réglages du dépôt GitHub, créer les environnements `pypi` et `testpypi`,
   relecteur exigé : le porteur (la publication attend son approbation). Dans l'environnement `pypi`, la **règle de
   déploiement** (étiquettes autorisées) n'accepte que le motif `sdk-v*` : un lancement manuel depuis une branche, ou
   une étiquette d'un autre motif, n'obtient jamais le jeton OIDC de PyPI — l'éditeur de confiance ne connaît que le
   dépôt, le workflow et l'environnement, jamais le ref : seul l'environnement l'arrête. L'environnement `testpypi`
   peut tout accepter : il ne publie que sur l'index d'essai.
4. **Étiquettes protégées** : ajouter `refs/tags/sdk-v*` à la règle du dépôt qui protège les étiquettes
   `refs/tags/v*` (ou créer une règle dédiée) : personne ne crée, ne déplace ni ne supprime une étiquette `sdk-v*`
   sans revue — sans elle, l'étiquette qui déclenche la publication resterait libre à quiconque peut pousser.
5. **Publication à l'essai** : lancer `publish-sdk.yml` par `workflow_dispatch` (onglet Actions) depuis le ref voulu :
   l'environnement `testpypi` publie sur TestPyPI. C'est le seul geste pour vérifier la chaîne avant PyPI.
6. **Publication réelle** : pousser l'étiquette `sdk-vX.Y.Z` sur `main` — le nom doit être exactement celui de
   `version` dans `sdk/pyproject.toml`, et le job `build` le contrôle deux fois : contre `sdk/pyproject.toml` avant
   de construire, puis contre les fichiers réellement construits dans `dist/` (une roue et un sdist
   `shelfhold_sdk-<version>`, rien d'autre) — l'environnement `pypi` publie sur PyPI après relecture.
7. **Un numéro publié ne se republie jamais** : relancer le workflow sur une étiquette déjà publiée échoue (PyPI le
   refuse) ; un correctif sort sous un nouveau numéro, jamais en écrasant.

Rappel de versions : la publication ne change pas celle de l'application (`v1.3.0` à M6, D24) ; le shim `api` 1 de
l'hôte (`shelfhold.sdk`) charge les deux numéros pendant 12 mois après la publication (D11, ENF-OPS-008).
