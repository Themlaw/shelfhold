# Publier une version

Une release est une action manuelle du mainteneur, en quatre étapes. Aucun jeton n'a le droit de créer une PR.

1. **Préparer la PR de version** (titre `chore(release): X.Y.Z`) :
   - `CHANGELOG.md` : nouvelle section `## X.Y.Z - AAAA-MM-JJ` avec les changements, et la ligne
     `Migration : oui|non, réversible : oui|non` (ENF-OPS-002) ;
   - la version dans `backend/pyproject.toml` et `backend/src/shelfhold/__init__.py` (`__version__`) ;
   - le champ `info.version` de `open-api/openapi.json` (sinon le job `openapi` échoue).
2. **Fusionner la PR** : `ci-success` doit être vert sur le commit fusionné, l'image `sha-<commit>` est alors publiée sur GHCR.
3. **Créer la release** (le tag `vX.Y.Z` est créé sur `main`) :
   ```sh
   gh release create vX.Y.Z --target main --title vX.Y.Z --notes-file <(extrait de la section du CHANGELOG)
   ```
   ou `--notes-from-tag` avec un tag annoté porteur des notes.
4. **Le workflow `release.yml`** se déclenche sur `release: published` et, dans un seul job `packages: write` :
   vérifie que le tag est `vX.Y.Z`, résout son commit, attend (borné) un `ci-success` vert sur ce commit, puis ajoute les tags
   `X.Y.Z` et `X.Y` au **digest déjà publié** (`sha-<commit>`) : jamais de reconstruction, jamais de `latest`.
   `X.Y.Z` est immuable (refus s'il existe avec un autre digest) ; `X.Y` ne recule jamais.
5. **Assets de release** (EF-INT-027) : un second job de `release.yml` (`schema-assets`, `contents: write`) génère `schema.sql` et
   `data-dictionary.md` avec `.github/scripts/release-assets.sh` et les attache à la release (`gh release upload --clobber`).
   `schema.sql` vient des migrations Alembic appliquées à une base vide ; le dictionnaire ajoute une description par table et par colonne
   (`backend/src/shelfhold/docgen/schema_descriptions.toml`) : un test échoue si l'une manque. Ni l'un ni l'autre ne s'écrit à la main.

**Reprise** : si le job a échoué (CI encore rouge, image absente, erreur du registre), relancer le workflow `release` depuis l'onglet
Actions (`workflow_dispatch`, entrée `tag` = `vX.Y.Z`). Le script est idempotent.
