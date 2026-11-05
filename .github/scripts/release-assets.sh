#!/usr/bin/env bash
# Generates the release assets schema.sql and data-dictionary.md (EF-INT-027) from the Alembic
# migrations applied to an empty database. Usage: release-assets.sh <output directory>.
# Nothing is written by hand: the dictionary fails if a table or a column has no description.
set -euo pipefail
out="${1:?usage: release-assets.sh <output directory>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
uv run --project "${here}/backend" --locked python -m shelfhold.docgen.release_assets "$out"
for name in schema.sql data-dictionary.md; do
  [ -s "${out}/${name}" ] || { echo "::error::${name} was not generated" >&2; exit 1; }
done
