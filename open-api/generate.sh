#!/usr/bin/env bash
# Regenerate open-api/openapi.json (from FastAPI) and the TS client (frontend/src/lib/api/generated).
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
uv run --locked --project "$root/backend" python "$root/open-api/export_schema.py" "$root/open-api/openapi.json"
npm --prefix "$root/frontend" run --silent api:generate
