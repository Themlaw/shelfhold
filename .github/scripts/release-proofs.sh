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
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

index="$(docker buildx imagetools inspect "${IMAGE}:${VERSION}" --format '{{.Manifest.Digest}}')"
raw="$(docker buildx imagetools inspect "${IMAGE}:${VERSION}" --raw)"
platform_digest() {
  jq -r --arg arch "$1" '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == $arch)][0].digest // empty' <<< "$raw"
}
amd64="$(platform_digest amd64)"
arm64="$(platform_digest arm64)"
[ -n "$amd64" ] && [ -n "$arm64" ] || { echo "::error::${IMAGE}:${VERSION} lacks a platform"; exit 1; }

# Statements of a digest whose predicate type starts with $2, decoded from the DSSE bundles.
statements() {
  local digest="$1" prefix="$2" file
  (cd "$work" && gh attestation download "oci://${IMAGE}@${digest}" --repo "$REPO" >/dev/null)
  file="$work/${digest/:/_}.jsonl"
  [ -f "$file" ] || file="$work/${digest}.jsonl"
  jq -c --arg prefix "$prefix" '.dsseEnvelope.payload | @base64d | fromjson | select(.predicateType | startswith($prefix))' "$file"
}

for digest in "$index" "$amd64" "$arm64"; do
  gh attestation verify "oci://${IMAGE}@${digest}" --repo "$REPO" --predicate-type https://slsa.dev/provenance/v1 >/dev/null
  echo "verified provenance of ${digest}"
done
for digest in "$amd64" "$arm64"; do
  gh attestation verify "oci://${IMAGE}@${digest}" --repo "$REPO" --predicate-type https://spdx.dev/Document/v2.3 >/dev/null
  echo "verified SBOM of ${digest}"
done

: > "$OUT/provenance.intoto.jsonl"
for digest in "$index" "$amd64" "$arm64"; do
  statements "$digest" https://slsa.dev/provenance >> "$OUT/provenance.intoto.jsonl"
done
statements "$amd64" https://spdx.dev/ | jq -c -s '.[0].predicate' > "$OUT/sbom.spdx.json"
statements "$arm64" https://spdx.dev/ | jq -c -s '.[0].predicate' > "$OUT/sbom-arm64.spdx.json"
for f in provenance.intoto.jsonl sbom.spdx.json sbom-arm64.spdx.json; do
  [ -s "$OUT/$f" ] && [ "$(cat "$OUT/$f")" != null ] || { echo "::error::empty ${f}"; exit 1; }
done
