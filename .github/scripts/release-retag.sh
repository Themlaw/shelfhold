#!/usr/bin/env bash
# Adds the version tags X.Y.Z and X.Y to the image ALREADY published for a release tag (digest of
# sha-<long>, built and tested by the ci run of the tagged commit): no rebuild, no "latest".
# Idempotent. X.Y.Z is immutable (refused if it exists with another digest, checked before anything
# is written); X.Y is the floating minor tag and never moves backwards.
set -euo pipefail
: "${IMAGE:?}" "${REPO:?}" "${TAG:?}" "${GH_TOKEN:?}"
IMAGE="${IMAGE,,}"
[[ "$TAG" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || { echo "::error::tag must be vX.Y.Z, got ${TAG}"; exit 1; }
VERSION="${TAG#v}"
minor="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}"
deadline=$((SECONDS + ${WAIT_SECONDS:-2400}))

# Prints the digest of a ref; prints nothing when the ref does not exist (404 / unknown manifest);
# any other failure (auth, network, 5xx) fails the script rather than being read as "absent".
digest_of() {
  local out
  if out="$(docker buildx imagetools inspect "$1" --format '{{.Manifest.Digest}}' 2>&1)"; then
    printf '%s\n' "$out" | tail -n1
  elif grep -qiE 'not found|404|manifest unknown|name unknown' <<< "$out"; then
    return 0
  else
    echo "::error::cannot inspect $1: ${out}" >&2; return 1
  fi
}

# 0. the commit the tag points to (annotated or lightweight)
SHA="$(gh api "repos/${REPO}/commits/${TAG}" --jq .sha)"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::cannot resolve ${TAG} to a commit"; exit 1; }
echo "${TAG} -> ${SHA}"

# 1. the commit must have passed ci-success (wait, bounded, while it is not concluded)
while :; do
  ci="$(gh api "repos/${REPO}/commits/${SHA}/check-runs?check_name=ci-success&filter=latest" \
    --jq '.check_runs[0].conclusion // "pending"')"
  [ "$ci" = success ] && break
  [ "$ci" = pending ] || { echo "::error::ci-success is ${ci} on ${SHA}"; exit 1; }
  [ "$SECONDS" -lt "$deadline" ] || { echo "::error::ci-success still pending on ${SHA}"; exit 1; }
  echo "waiting for ci-success on ${SHA}"; sleep 60
done

# 2. the image of that commit
src="${IMAGE}:sha-${SHA}"
digest="$(digest_of "$src")"
while [ -z "$digest" ]; do
  [ "$SECONDS" -lt "$deadline" ] || { echo "::error::${src} still absent after the bounded wait"; exit 1; }
  echo "waiting for ${src}"; sleep 60
  digest="$(digest_of "$src")"
done

# 3. X.Y.Z: verified BEFORE anything is written
existing="$(digest_of "${IMAGE}:${VERSION}")"
if [ -n "$existing" ] && [ "$existing" != "$digest" ]; then
  echo "::error::${IMAGE}:${VERSION} already exists with digest ${existing}, refusing to rewrite it"; exit 1
fi

# 4. X.Y only moves forward: skip it when a higher patch of this minor is already published
name="${IMAGE#ghcr.io/}"
token="$(curl -fsS -u "token:${GH_TOKEN}" "https://ghcr.io/token?service=ghcr.io&scope=repository:${name}:pull" | jq -r .token)"
tags="$(curl -fsS -H "Authorization: Bearer ${token}" "https://ghcr.io/v2/${name}/tags/list?n=1000" | jq -r '.tags[]?')"
highest="$( { grep -E "^${minor//./\\.}\.[0-9]+$" <<< "$tags" || true; echo "$VERSION"; } | sort -V | tail -n1)"
args=(-t "${IMAGE}:${VERSION}")
if [ "$highest" = "$VERSION" ]; then args+=(-t "${IMAGE}:${minor}")
else echo "keeping ${minor} on ${highest} (newer than ${VERSION})"; fi
docker buildx imagetools create "${args[@]}" "${IMAGE}@${digest}"
[ "$(digest_of "${IMAGE}:${VERSION}")" = "$digest" ] || { echo "::error::tag ${VERSION} does not point to ${digest}"; exit 1; }
echo "tagged ${args[*]} -> ${digest}"
