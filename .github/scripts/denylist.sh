#!/usr/bin/env bash
# Fails if a regex from DENYLIST_NAMES (one per line) matches the name or content
# of a git-tracked file. Never prints the regex or the matched text.
set -euo pipefail

if [ -z "${DENYLIST_NAMES:-}" ]; then
  echo "::warning::DENYLIST_NAMES is empty or missing (fork PR?): check skipped, rerun before merge."
  exit 0
fi

status=0
n=0
while IFS= read -r pattern || [ -n "$pattern" ]; do
  [ -z "$pattern" ] && continue
  n=$((n + 1))
  hits=$(git ls-files | grep -i -E -e "$pattern" || true)
  hits+=$'\n'$(git grep -I -i -l -E -e "$pattern" || true)
  hits=$(printf '%s\n' "$hits" | sed '/^$/d' | sort -u)
  if [ -n "$hits" ]; then
    status=1
    echo "::error::denylist rule number $n matches:"
    printf '%s\n' "$hits" | sed 's/^/  - /'
  fi
done <<< "$DENYLIST_NAMES"

echo "denylist: $n rule(s) evaluated"
exit $status
