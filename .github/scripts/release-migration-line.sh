#!/usr/bin/env bash
# Appends (or replaces) the "Migration : oui/non, réversible : oui/non" line (ENF-OPS-002) in the
# body of the release PR, computed from the Alembic revisions changed since the last v* tag, then
# checks that the line is present. Reversible = every changed revision defines a real downgrade() (not just a docstring or pass).
set -euo pipefail
: "${PR:?}" "${GH_TOKEN:?}"
marker='Migration : '
last="$(git describe --tags --abbrev=0 --match 'v*' 2>/dev/null || git hash-object -t tree /dev/null)"
mapfile -t revs < <(git diff --name-only --diff-filter=AM "$last" HEAD -- 'backend/alembic/versions/*.py')
migration=non; reversible=oui
if [ "${#revs[@]}" -gt 0 ]; then
  migration=oui
  for f in "${revs[@]}"; do
    python3 -I "$(dirname "$0")/is-reversible.py" "$f" || reversible=non
  done
fi
line="${marker}${migration}, réversible : ${reversible}"
body="$(gh pr view "$PR" --json body --jq .body | grep -v "^${marker}" || true)"
gh pr edit "$PR" --body "$(printf '%s\n\n%s\n' "$body" "$line")"
gh pr view "$PR" --json body --jq .body | grep -Eq "^${marker}(oui|non), réversible : (oui|non)$" \
  || { echo "::error::release PR body lacks the migration line"; exit 1; }
echo "$line"
