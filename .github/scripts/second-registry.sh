#!/usr/bin/env bash
# Decides whether the images are also published and signed on the second registry (ENF-GOV-004).
#
#   second-registry.sh detect
#
# The second registry is optional: it is used only when the secret CODEBERG_TOKEN exists. The
# workflow hands over HAS_TOKEN (`secrets.CODEBERG_TOKEN != ''`), never the token itself: this
# script cannot print what it does not receive. Without the token it writes a clear line to the job
# summary and exits 0; GHCR is signed in every case.
#
# Inputs (environment): HAS_TOKEN (true|false), SECOND_REGISTRY (repository variable, may be
# empty; default `codeberg.org/<owner>`), OWNER (repository owner), REPO_NAME (repository name).
# Outputs ($GITHUB_OUTPUT): enabled, and when enabled: host, image.
set -euo pipefail
: "${GITHUB_OUTPUT:?}" "${GITHUB_STEP_SUMMARY:?}"
[ "${1:-}" = detect ] || { echo "usage: second-registry.sh detect" >&2; exit 2; }

summary() { printf '%s\n' "$*" >> "$GITHUB_STEP_SUMMARY"; }

if [ "${HAS_TOKEN:-false}" != true ]; then
  summary "### Second registry: skipped"
  summary "The secret \`CODEBERG_TOKEN\` is absent: the images were signed and published on GHCR only (ENF-GOV-004 stays open). Nothing failed."
  echo "enabled=false" >> "$GITHUB_OUTPUT"
  exit 0
fi

owner="${OWNER:-}"; repo="${REPO_NAME:-}"
registry="${SECOND_REGISTRY:-}"
[ -n "$registry" ] || registry="codeberg.org/${owner,,}"
registry="${registry,,}"
# host[:port]/path...: lowercase letters, digits, dots and dashes; no scheme, no `..`, no space
re='^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)+$'
if [[ "$registry" =~ $re ]] && [[ "$registry" != *..* ]] && [[ "${repo,,}" =~ ^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$ ]]; then
  :
else
  echo "::error::SECOND_REGISTRY is not a valid registry path (expected host[:port]/owner, default codeberg.org/<owner>)"
  summary "### Second registry: configuration error"
  summary "The variable \`SECOND_REGISTRY\` is not a valid registry path. Nothing was published there."
  exit 1
fi
image="${registry}/${repo,,}"
{
  echo "enabled=true"
  echo "host=${registry%%/*}"
  echo "image=${image}"
} >> "$GITHUB_OUTPUT"
summary "### Second registry: enabled"
summary "Images are copied (digest preserved) and signed on \`${image}\`."
