#!/usr/bin/env bash
# Adds the version tags X.Y.Z and X.Y to the image ALREADY published for this commit (digest of
# sha-<long>, built and tested by the ci run of the same commit): no rebuild, never a floating "latest".
# X.Y.Z is immutable (fails if it exists with another digest); X.Y is the floating minor tag.
set -euo pipefail
: "${IMAGE:?}" "${SHA:?}" "${VERSION:?}"
IMAGE="${IMAGE,,}"
[[ "$VERSION" =~ ^([0-9]+)\.([0-9]+)\.[0-9]+$ ]] || { echo "::error::bad version ${VERSION}"; exit 1; }
minor="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}"
digest_of() { docker buildx imagetools inspect "$1" --format '{{.Manifest.Digest}}' 2>/dev/null || true; }
src="${IMAGE}:sha-${SHA}"
deadline=$((SECONDS + ${WAIT_SECONDS:-2400}))
digest="$(digest_of "$src")"
while [ -z "$digest" ]; do
  [ "$SECONDS" -lt "$deadline" ] || { echo "::error::${src} still absent after the bounded wait"; exit 1; }
  echo "waiting for ${src} (ci run of the same commit)"; sleep 60
  digest="$(digest_of "$src")"
done
existing="$(digest_of "${IMAGE}:${VERSION}")"
if [ -n "$existing" ] && [ "$existing" != "$digest" ]; then
  echo "::error::${IMAGE}:${VERSION} already exists with digest ${existing}, refusing to rewrite it"; exit 1
fi
docker buildx imagetools create -t "${IMAGE}:${VERSION}" -t "${IMAGE}:${minor}" "${IMAGE}@${digest}"
[ "$(digest_of "${IMAGE}:${VERSION}")" = "$digest" ] || { echo "::error::tag ${VERSION} does not point to ${digest}"; exit 1; }
echo "tagged ${VERSION} and ${minor} -> ${digest}"
