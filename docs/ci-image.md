# Pipeline de l'image (ENF-PORT-001, ENF-QUAL-007, ENF-SEC-007)

| Workflow / job | Quand | Jeton | Rôle |
| --- | --- | --- | --- |
| `ci.yml` / `image-pr` | `pull_request`, seulement si `backend/`, `frontend/`, `docker/`, `e2e/`, `compose*` ou `.github/workflows/image*` changent | `contents: read`, `packages: read` | `image.yml` : build natif **amd64 seul**, taille <= 300 Mo, e2e |
| `ci.yml` / `image-main` | `push` sur `main` | `packages: write` | idem sur amd64 **et arm64**, plus rafraîchissement du cache de couches dans GHCR |
| `ci.yml` / `image-publish` | `push` sur `main`, après `image-main`, `frontend` et `openapi` (ou filtrés) | `packages: write` | `image-publish.yml` : publie les archives OCI **déjà testées**, puis le manifeste multi-arch (`edge`, `sha-<long>`) |
| `ci.yml` / `scan` | tout run où l'image est construite | `security-events: write` | Grype sur l'archive amd64 testée, SARIF vers l'onglet Sécurité ; **informatif** (hors `ci-success`, `fail-build` désactivé : une CVE publiée demain ne doit pas casser une PR sans rapport) |
| `codeql.yml` | `pull_request`, `push` sur `main`, hebdomadaire | `security-events: write` | CodeQL Python et TypeScript (dépôt public : code scanning gratuit) |
| `cleanup.yml` | hebdomadaire, ou à la main (`dry-run` par défaut) | `packages: write` | supprime les versions GHCR dont **tous** les tags sont `pr-*` et plus vieilles que 14 jours ; jamais une version sans tag (signatures, attestations) ni les manifestes par plateforme, qui portent le tag `build-<sha>-<arch>` |

- **Attestations (ENF-SEC-007, palier M1b).** Le job `merge` de `image-publish.yml` est le seul à détenir `id-token: write` et
  `attestations: write` (l'appelant `ci.yml` les accorde à ce seul appel). Il attache une provenance SLSA au manifeste multi-arch et
  à chaque image de plateforme, et un SBOM SPDX à chaque image de plateforme (`gh attestation verify` réussit pour les deux
  architectures).
- **Signatures cosign (ENF-SEC-007, palier M3).** Le même job `merge` signe, sans clé (OIDC), le manifeste multi-arch et les deux
  images de plateforme, puis vérifie ses propres signatures. **Second registre (ENF-GOV-004, optionnel)** : si le secret
  `CODEBERG_TOKEN` existe, le job copie le manifeste (digest conservé) vers `vars.SECOND_REGISTRY` (`codeberg.org/<owner>` par
  défaut), signe les mêmes digests et vérifie ; sinon il l'écrit dans son résumé (« Second registry: skipped ») et réussit. `release.yml`
  (`second-registry`, sans `id-token`) n'ajoute `X.Y.Z` et `X.Y` là-bas que pour un digest déjà signé. Le secret n'atteint que
  `docker/login-action`, jamais une exécution `pull_request`. Vérification : [verifier-les-images.md](admin/verifier-les-images.md).
- **Scorecard et site.** `scorecard.yml` (hebdomadaire, SARIF vers l'onglet Sécurité, sans `id-token`) ; `docs.yml` construit
  `docs/` avec Zensical en mode strict sur chaque PR qui touche `docs/` et publie sur GitHub Pages à chaque release (job `deploy` :
  `pages: write` et `id-token: write`, aucun code du dépôt).
- **`packages: write` n'atteint jamais une exécution `pull_request`**, y compris pour une branche du dépôt
  (`test_no_job_reachable_on_pull_request_has_packages_write`). C'est pourquoi la publication est dans un second
  workflow réutilisable : un workflow appelé ne peut pas déclarer plus de droits que son appelant, même pour un job sauté.
- **On publie ce qui a été testé.** `image.yml` exporte une archive OCI par architecture ; l'e2e la charge
  (`docker/load-oci.sh`, skopeo : `docker load` ne lit pas les archives OCI hors magasin containerd) sur un runner de la même architecture ; `image-publish.yml` la pousse telle quelle avec `skopeo copy`
  et vérifie que le digest poussé est celui de l'archive. Rien n'est reconstruit. Le tag temporaire
  `build-<sha>-<arch>` sert de support à ce push (un push vers un digest seul est impossible) ; le manifeste public
  référence les digests. Ces tags `build-*` sont des enfants de l'index `sha-*` : aucun nettoyage dédié n'est nécessaire. La rétention des `sha-*` (et des tags `edge`/versions) est à prévoir plus tard.
- **Cache.** `ghcr.io/themlaw/shelfhold-cache:<arch>`, lu avec le `GITHUB_TOKEN` sur les PR du dépôt, écrit
  (`mode=max`) seulement sur `main`. **PR de fork** : le jeton ne peut pas lire le paquet, le cache est manqué
  (accepté) ; le build est plus lent, jamais faux.
- Le paquet GHCR doit être public après le premier push pour que le cache et l'image soient lisibles sans login.
- Les jobs `image-pr`/`image-main` n'ont pas de bloc `permissions` dans `image.yml` : ils héritent du jeton de l'appelant
  (lecture seule sur PR). Les jobs e2e le réduisent à `contents: read`.
