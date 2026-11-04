#!/usr/bin/env bash
# Deletes expired temporary build-* tags of the image (see ghcr-cleanup.jq). The per-architecture
# build-<sha>-<arch> version IS the platform manifest of the multi-arch index: it is kept as long
# as an index carrying a kept tag references it. Dry-run unless DRY_RUN is exactly "false".
set -euo pipefail
: "${OWNER:?}" "${PACKAGE:?}" "${GH_TOKEN:?}"
OWNER="${OWNER,,}"
days="${MAX_AGE_DAYS:-7}"
here="$(cd "$(dirname "$0")" && pwd)"
kind="$(gh api "users/${OWNER}" --jq '.type')"
base="$([ "$kind" = Organization ] && echo "orgs/${OWNER}" || echo "users/${OWNER}")/packages/container/${PACKAGE}/versions"
gh api --paginate "${base}?per_page=100" --jq '.[]' | jq -s '.' > "$RUNNER_TEMP/versions.json"
cutoff="$(date -u -d "${days} days ago" +%Y-%m-%dT%H:%M:%SZ)"
# Digests referenced by every index that carries a tag other than build-*.
protected='[]'
while read -r digest; do
  children="$(docker buildx imagetools inspect --raw "ghcr.io/${OWNER}/${PACKAGE}@${digest}" \
    | jq -c '[.manifests[]?.digest]')" || { echo "::error::cannot inspect ${digest}"; exit 1; }
  protected="$(jq -c --argjson a "$protected" '$a + .' <<< "$children")"
done < <(jq -r '.[] | select([(.metadata.container.tags // [])[] | select(startswith("build-") | not)] | length > 0) | .name' "$RUNNER_TEMP/versions.json")
jq -c --arg cutoff "$cutoff" --argjson protected "$protected" -f "$here/ghcr-cleanup.jq" "$RUNNER_TEMP/versions.json" > "$RUNNER_TEMP/doomed.json"
jq -r '.[] | "\(.id) \(.name) \(.tags | join(","))"' "$RUNNER_TEMP/doomed.json" | while read -r id name tags; do
  echo "delete version ${id} (${name}; ${tags})"
  [ "${DRY_RUN:-true}" != false ] || gh api -X DELETE "${base}/${id}"
done
