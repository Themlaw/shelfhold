# Vérifier les images (signature et provenance)

Chaque image publiée par la CI (`edge`, `sha-<commit>`, puis les versions `X.Y.Z` et `X.Y`) est **signée avec cosign, sans clé** : la signature est faite par le workflow `image-publish.yml` du dépôt, avec le jeton OIDC de GitHub Actions, et inscrite dans le journal public Sigstore. Aucune clé privée n'existe, donc aucune ne peut fuir. Les tags (`edge`, `sha-<commit>`) ne sont posés qu'**après** la signature et sa vérification ; les images publiées avant ce palier ne sont pas signées. Le manifeste multi-architecture et chaque image de plateforme (amd64, arm64) sont signés.

**Les commandes ci-dessous demandent cosign ≥ 3** : les signatures sont écrites au format de cosign 3 (référents OCI) et un cosign 2 peut ne rien trouver.

Trois preuves, indépendantes les unes des autres :

| Preuve | Ce qu'elle dit | Commande |
| --- | --- | --- |
| Signature cosign | cette image a été publiée par le workflow du dépôt, sur `main` | `cosign verify` (ci-dessous) |
| Provenance SLSA | elle a été construite par ce workflow, à partir de ce commit | `gh attestation verify` |
| SBOM SPDX | voici ce qu'elle contient | `gh attestation verify --predicate-type https://spdx.dev/Document/v2.3` |

## Registre principal : GHCR (toujours signé)

```sh
cosign verify ghcr.io/themlaw/shelfhold@sha256:<digest> \
  --certificate-identity-regexp '^https://github\.com/Themlaw/shelfhold/\.github/workflows/image-publish\.yml@refs/heads/main$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Le digest se lit avec `docker buildx imagetools inspect ghcr.io/themlaw/shelfhold:<tag> --format '{{.Manifest.Digest}}'`. **Vérifiez le digest, pas le tag** : un tag peut être déplacé, un digest non. Un `cosign verify` qui réussit imprime la signature et le certificat ; un échec (code de sortie non nul) veut dire « ne déployez pas ».

## Second registre (optionnel)

Le second registre sert si GitHub retire le dépôt (ENF-GOV-004). Il est publié **seulement si le mainteneur a fourni le secret `CODEBERG_TOKEN`** ; sans lui, rien n'y est publié, rien n'échoue, et l'image reste signée sur GHCR. Les mêmes digests y sont copiés, signés par le même workflow, donc la même commande marche en changeant le nom :

```sh
cosign verify codeberg.org/<owner>/shelfhold@sha256:<digest> \
  --certificate-identity-regexp '^https://github\.com/Themlaw/shelfhold/\.github/workflows/image-publish\.yml@refs/heads/main$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

`<owner>` est le compte du second registre. L'identité attendue reste celle du **workflow GitHub** : c'est elle qui signe, quel que soit le registre. La vérification s'appuie sur Sigstore (Fulcio et Rekor), pas sur l'API de GitHub.

## Provenance et SBOM

```sh
gh attestation verify oci://ghcr.io/themlaw/shelfhold@sha256:<digest> --repo Themlaw/shelfhold
```

Les attestations sont aussi des assets de chaque release (`sbom.spdx.json`, `provenance.intoto.jsonl`).

## Pour le mainteneur : brancher le second registre

Rien n'est à changer dans le code ; ce sont des réglages du dépôt GitHub, réversibles :

| Réglage | Où | Valeur |
| --- | --- | --- |
| `CODEBERG_TOKEN` | secret du dépôt | jeton du compte du second registre, avec le droit d'écrire des paquets (jamais dans un fichier du dépôt) |
| `SECOND_REGISTRY` | variable du dépôt | `codeberg.org/<owner>` par défaut ; `<owner>` est le propriétaire du dépôt GitHub (en minuscules), à remplacer ici s'il diffère. L'hôte doit être `codeberg.org`, `docker.io` ou `quay.io` : le jeton n'est envoyé nulle part ailleurs, et un autre hôte fait échouer l'étape avec un message clair |
| `SECOND_REGISTRY_USER` | variable du dépôt, facultative | identifiant du compte ; par défaut le propriétaire du dépôt GitHub |

**Sans le jeton** : la CI fonctionne comme avant. `image-publish.yml` signe GHCR et écrit dans le résumé du job « Second registry: skipped » ; `release.yml` écrit la même chose. **Avec le jeton** : à chaque publication sur `main`, la CI copie le manifeste (digest conservé), signe les trois digests, puis vérifie les deux registres dans le même run ; à chaque release, elle ajoute `X.Y.Z` et `X.Y` sur GHCR **seulement** après avoir vérifié la signature du digest (sauf relance d'une version qui porte déjà ce digest), et sur le second registre **seulement** pour un digest déjà présent et signé (une valeur de `SECOND_REGISTRY` invalide fait échouer l'étape : l'erreur est voulue).

L'exigence ENF-GOV-004 (second registre, miroir du dépôt, vérificateur de mises à jour) est **reportée en M4** (décision du porteur, D5) et reste ouverte : la v1.0.0 est signée sur GHCR seul. Le second registre s'active dès que le jeton existe, sans changer le code.
