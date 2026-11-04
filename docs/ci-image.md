# Pipeline de l'image (ENF-PORT-001, ENF-QUAL-007, ENF-SEC-007)

| Workflow / job | Quand | Jeton | Rôle |
| --- | --- | --- | --- |
| `ci.yml` / `image-pr` | `pull_request` | `contents: read`, `packages: read` | `image.yml` : build natif amd64 et arm64, taille <= 300 Mo, e2e sur les deux architectures |
| `ci.yml` / `image-main` | `push` sur `main` | `packages: write` | idem, plus rafraîchissement du cache de couches dans GHCR |
| `ci.yml` / `image-publish` | `push` sur `main`, après `image-main` | `packages: write` | `image-publish.yml` : publie les archives OCI **déjà testées**, puis le manifeste multi-arch (`edge`, `sha-<long>`) |

- **`packages: write` n'atteint jamais une exécution `pull_request`**, y compris pour une branche du dépôt
  (`test_no_job_reachable_on_pull_request_has_packages_write`). C'est pourquoi la publication est dans un second
  workflow réutilisable : un workflow appelé ne peut pas déclarer plus de droits que son appelant, même pour un job sauté.
- **On publie ce qui a été testé.** `image.yml` exporte une archive OCI par architecture ; l'e2e la charge
  (`docker load`) sur un runner de la même architecture ; `image-publish.yml` la pousse telle quelle avec `skopeo copy`
  et vérifie que le digest poussé est celui de l'archive. Rien n'est reconstruit. Le tag temporaire
  `build-<sha>-<arch>` sert de support à ce push (un push vers un digest seul est impossible) ; le manifeste public
  référence les digests. Le nettoyage de ces tags relève de `cleanup.yml` (M1b, tags `pr-*` et `build-*`).
- **Cache.** `ghcr.io/themlaw/shelfhold-cache:<arch>`, lu avec le `GITHUB_TOKEN` sur les PR du dépôt, écrit
  (`mode=max`) seulement sur `main`. **PR de fork** : le jeton ne peut pas lire le paquet, le cache est manqué
  (accepté) ; le build est plus lent, jamais faux.
- Le paquet GHCR doit être public après le premier push pour que le cache et l'image soient lisibles sans login.
- Les jobs `image-pr`/`image-main` n'ont pas de bloc `permissions` dans `image.yml` : ils héritent du jeton de l'appelant
  (lecture seule sur PR). Les jobs e2e le réduisent à `contents: read`.
