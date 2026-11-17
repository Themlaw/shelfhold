#!/usr/bin/env bash
# Pushes one ref (the branch main, or a version tag) to the repository mirror (ENF-GOV-004).
#
#   publish-mirror.sh check   validate the target, print it, push nothing
#   publish-mirror.sh push    push REF_NAME (never forced: a diverged mirror fails loudly)
#
# Inputs (environment): OWNER, REPO_NAME, MIRROR_URL (repository variable, may be empty; default
# https://codeberg.org/<owner>/<repo>.git), REF_TYPE (branch|tag), REF_NAME (main | vX.Y.Z...), SHA
# (run from a full clone), and for `push` only: MIRROR_USER (the account of the mirror, its own
# variable: not the owner of the repository, not the user of another registry) and MIRROR_TOKEN.
# A tag is pushed only if it designates SHA (a deleted tag makes GitHub report the head of main).
# The token is handed to git through GIT_ASKPASS, and only `git push` has it in its environment: it
# is on no command line, in no URL, in no file of the checkout, and every other command runs
# without it. The host must be codeberg.org: a token is never sent anywhere else.
set -euo pipefail
# the token leaves the environment at once: every command below runs without it but `git push`
token="${MIRROR_TOKEN:-}"
unset MIRROR_TOKEN
mode="${1:-}"
case "$mode" in check | push) ;; *) echo "usage: publish-mirror.sh check|push" >&2; exit 2 ;; esac

owner="${OWNER:-}"; repo="${REPO_NAME:-}"
url="${MIRROR_URL:-}"
[ -n "$url" ] || url="https://codeberg.org/${owner,,}/${repo,,}.git"
re='^https://codeberg\.org/[a-z0-9]([a-z0-9._-]*[a-z0-9])?/[a-z0-9]([a-z0-9._-]*[a-z0-9])?\.git$'
if ! [[ "${url,,}" =~ $re ]] || [[ "$url" == *..* ]]; then
  echo "::error::MIRROR_URL must be https://codeberg.org/<owner>/<repository>.git"
  exit 1
fi

ref_type="${REF_TYPE:-}"; ref_name="${REF_NAME:-}"; sha="${SHA:-}"
case "$ref_type" in
  branch) [ "$ref_name" = main ] || { echo "::error::only main is mirrored as a branch"; exit 1; }
          dest="refs/heads/main" ;;
  tag)    [[ "$ref_name" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[1-9][0-9]*)?$ ]] \
            || { echo "::error::tag must be vX.Y.Z or vX.Y.Z-rc.N"; exit 1; }
          dest="refs/tags/${ref_name}" ;;
  *) echo "::error::REF_TYPE must be branch or tag"; exit 1 ;;
esac
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::SHA is not a commit id"; exit 1; }
if [ "$ref_type" = tag ]; then
  tagged="$(git rev-parse "refs/tags/${ref_name}^{commit}" 2>/dev/null)" || tagged=""
  if [ "$tagged" != "$sha" ]; then
    echo "::error::the tag ${ref_name} does not designate the commit of this run: nothing was pushed"
    exit 1
  fi
fi

if [ "$mode" = check ]; then
  echo "target=${url} ref=${dest}"
  exit 0
fi

: "${token:?MIRROR_TOKEN is required}" "${MIRROR_USER:?MIRROR_USER is required}"
[[ "$MIRROR_USER" =~ ^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$ ]] || { echo "::error::MIRROR_USER is not a user name"; exit 1; }
export MIRROR_USER
askpass="$(mktemp)"
trap 'rm -f "$askpass"' EXIT
chmod 700 "$askpass"
cat > "$askpass" <<'ASK'
#!/bin/sh
case "$1" in
  Username*) printf '%s' "$MIRROR_USER" ;;
  *) printf '%s' "$MIRROR_TOKEN" ;;
esac
ASK
export GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0
MIRROR_TOKEN="$token" git push "$url" "${sha}:${dest}"
echo "Mirror: ${dest} is at ${sha:0:12} on the second forge." >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
