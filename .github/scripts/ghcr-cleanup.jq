# Selects the GHCR package versions that cleanup.yml may delete: versions whose tags are ALL
# temporary build-* tags, older than the cutoff, and not referenced by a kept image.
# Input: array of package versions (GitHub API). Args: $cutoff (ISO 8601), $protected (digests).
# Never selected: untagged versions, and any version carrying edge, sha-*, X.Y.Z or X.Y.
[ .[]
  | (.metadata.container.tags // []) as $tags
  | select(($tags | length) > 0)
  | select(all($tags[]; startswith("build-")))
  | select(.created_at < $cutoff)
  | select(.name as $d | ($protected | index($d)) == null)
  | {id, name, tags: $tags} ]
