#!/usr/bin/env bash
# Extracts the proofs of a release from the attestations of the published image (ENF-SEC-007):
#   sbom.spdx.json          SBOM (SPDX) of the linux/amd64 image
#   sbom-arm64.spdx.json    SBOM (SPDX) of the linux/arm64 image
#   provenance.intoto.jsonl in-toto statements of the SLSA provenance (manifest list + both images)
# Each digest is verified with `gh attestation verify` before anything is read from it, for the
# provenance and for the SBOM. Idempotent: it only reads the registry and writes under $OUT.
set -euo pipefail
: "${IMAGE:?}" "${REPO:?}" "${TAG:?}" "${OUT:?}" "${GH_TOKEN:?}"
IMAGE="${IMAGE,,}"
[[ "$TAG" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || { echo "::error::tag must be vX.Y.Z, got ${TAG}"; exit 1; }
VERSION="${TAG#v}"
mkdir -p "$OUT"

index="$(docker buildx imagetools inspect "${IMAGE}:${VERSION}" --format '{{.Manifest.Digest}}')"
raw="$(docker buildx imagetools inspect "${IMAGE}:${VERSION}" --raw)"
platform_digest() {
  jq -r --arg arch "$1" '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == $arch)][0].digest // empty' <<< "$raw"
}
amd64="$(platform_digest amd64)"
arm64="$(platform_digest arm64)"
[ -n "$amd64" ] && [ -n "$arm64" ] || { echo "::error::${IMAGE}:${VERSION} lacks a platform"; exit 1; }

SHA="$(gh api "repos/${REPO}/commits/${TAG}" --jq .sha)"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::cannot resolve ${TAG} to a commit"; exit 1; }

# Verifies the attestations of a digest (predicate type $2) and prints their in-toto statements,
# one per line: what is read is what was verified (signed by image-publish.yml, run on main for
# the tagged commit), never a separate download.
verified_statements() {
  gh attestation verify "oci://${IMAGE}@$1" --repo "$REPO" \
    --signer-workflow "${REPO}/.github/workflows/image-publish.yml" \
    --source-ref refs/heads/main --source-digest "$SHA" \
    --predicate-type "$2" --format json | jq -c '.[].verificationResult.statement'
}

: > "$OUT/provenance.intoto.jsonl"
for digest in "$index" "$amd64" "$arm64"; do
  verified_statements "$digest" https://slsa.dev/provenance/v1 >> "$OUT/provenance.intoto.jsonl"
  echo "verified provenance of ${digest}"
done
verified_statements "$amd64" https://spdx.dev/Document/v2.3 | jq -c -s 'map(.predicate) | .[0]' > "$OUT/sbom.spdx.json"
verified_statements "$arm64" https://spdx.dev/Document/v2.3 | jq -c -s 'map(.predicate) | .[0]' > "$OUT/sbom-arm64.spdx.json"
for f in provenance.intoto.jsonl sbom.spdx.json sbom-arm64.spdx.json; do
  [ -s "$OUT/$f" ] && [ "$(cat "$OUT/$f")" != null ] || { echo "::error::empty ${f}"; exit 1; }
done
