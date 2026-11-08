#!/usr/bin/env bash
# fetch-tool.sh URL SHA256 DEST: puts a release asset at DEST, whatever its origin.
# A file already there (restored from the Actions cache) is trusted for nothing: it is kept only if
# its sum matches, otherwise it is fetched again. The sum is checked last, on every path.
set -euo pipefail

url="$1"
sha256="$2"
dest="$3"

mkdir -p "$(dirname "$dest")"
if [ -f "$dest" ] && echo "${sha256}  ${dest}" | sha256sum --check --strict --status; then
  echo "${dest}: restored from the cache, sum verified"
  exit 0
fi
rm -f "$dest"
curl -sSfL --retry 6 --retry-all-errors --retry-delay 20 -o "$dest" "$url"
echo "${sha256}  ${dest}" | sha256sum --check --strict
