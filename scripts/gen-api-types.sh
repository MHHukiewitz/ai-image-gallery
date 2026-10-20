#!/usr/bin/env bash
# Regenerates web/src/lib/api-types.ts from the API's OpenAPI schema. `make types` runs this.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"

# gallery.main reads the settings when it is imported. The schema doesn't depend on their values,
# so placeholders are enough when the caller has no .env.
export SUPABASE_URL="${SUPABASE_URL:-http://127.0.0.1:54321}"
export SUPABASE_PUBLISHABLE_KEY="${SUPABASE_PUBLISHABLE_KEY:-placeholder-publishable-key}"

schema_file="$(mktemp)"
trap 'rm -f "$schema_file"' EXIT

(
  cd "$repo_root/api"
  uv run python - "$schema_file" <<'PY'
import json
import sys
from pathlib import Path

from gallery.main import create_app

Path(sys.argv[1]).write_text(json.dumps(create_app().openapi(), indent=2) + "\n")
PY
)

# Run the locally installed binary, not `npx`, so a missing `npm ci` fails instead of
# downloading a version nobody pinned.
(
  cd "$repo_root/web"
  mkdir -p src/lib
  node_modules/.bin/openapi-typescript "$schema_file" --output src/lib/api-types.ts
)
