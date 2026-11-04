#!/usr/bin/env bash
# Fails if a regex from DENYLIST_NAMES (one per line, ERE) matches the name or content
# of a git-tracked file. Never prints the regex or the matched text.
# A missing secret is tolerated only on fork PRs (IS_FORK=true).
set -euo pipefail

if [ -z "${DENYLIST_NAMES:-}" ]; then
  if [ "${IS_FORK:-false}" = "true" ]; then
    echo "::warning::DENYLIST_NAMES is unavailable on a fork PR: check skipped, rerun before merge."
    exit 0
  fi
  echo "::error::DENYLIST_NAMES is empty or missing: the check cannot run."
  exit 1
fi

# Run a grep-like command; only exit codes 0 (match) and 1 (no match) are accepted.
run_grep() {
  local rc=0
  "$@" || rc=$?
  if [ "$rc" -gt 1 ]; then
    echo "::error::grep failed with exit code $rc (invalid regex?)" >&2
    exit 1
  fi
  return 0
}

# Commit messages to scan too (git log arguments, e.g. "base..head" or "-1 <sha>"); optional.
messages=""
if [ -n "${DENYLIST_LOG_ARGS:-}" ]; then
  # shellcheck disable=SC2086
  messages=$(git log --format=%B $DENYLIST_LOG_ARGS)
fi

status=0
n=0
while IFS= read -r pattern || [ -n "$pattern" ]; do
  pattern=${pattern//$'\r'/}
  [ -z "$pattern" ] && continue
  n=$((n + 1))
  hits=$(git ls-files | run_grep grep -i -E -e "$pattern")
  hits+=$'\n'$(run_grep git grep -I -i -l -E -e "$pattern")
  hits=$(printf '%s\n' "$hits" | sed '/^$/d' | sort -u)
  if [ -n "$messages" ] && grep -q -i -E -e "$pattern" <<< "$messages"; then
    status=1
    echo "::error::denylist rule number $n matches a commit message"
  fi
  if [ -n "$hits" ]; then
    status=1
    echo "::error::denylist rule number $n matches:"
    printf '%s\n' "$hits" | sed 's/^/  - /'
  fi
done <<< "$DENYLIST_NAMES"

echo "denylist: $n rule(s) evaluated"
exit $status
