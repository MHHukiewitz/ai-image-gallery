#!/usr/bin/env bash
# Deploys the gallery to ops-server: rsync the tree, build and start it there, wait for health.
# The runbook is docs/deploy.md. This machine needs git, rsync, ssh and curl.
#
#   scripts/deploy.sh               deploy the current checkout
#   DRY_RUN=1 scripts/deploy.sh     print every command instead of running it
#
# Overrides: OPS_HOST (default ops@5.75.189.90), OPS_DEST (/opt/ai-gallery), HEALTH_URL, and
# HEALTH_TIMEOUT in seconds (60).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPS_HOST="${OPS_HOST:-ops@5.75.189.90}"
OPS_DEST="${OPS_DEST:-/opt/ai-gallery}"
HEALTH_URL="${HEALTH_URL:-https://gallery.hukiewitz.com/api/health?deep=1}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"
DRY_RUN="${DRY_RUN:-0}"
COMPOSE_FILE="deploy/compose.prod.yml"

# Values in the server .env that mean it was copied from a template and never filled in, or
# that it still points at a local stack. Only uncommented lines count.
PLACEHOLDER_PATTERN='CHANGE_ME|127\.0\.0\.1|localhost|host\.docker\.internal'

die() {
  echo "error: $*" >&2
  exit 1
}

# Every command that touches the server goes through here, so DRY_RUN covers all of them.
run() {
  if [[ "$DRY_RUN" == 1 ]]; then
    printf '[dry-run]'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

for tool in git rsync ssh curl; do
  command -v "$tool" > /dev/null || die "$tool is not installed"
done

# rsync --delete below mirrors the tree into OPS_DEST, so a short or empty path is dangerous.
while [[ "$OPS_DEST" == */ ]]; do OPS_DEST="${OPS_DEST%/}"; done
[[ "$OPS_DEST" == /?* ]] || die "OPS_DEST must be an absolute path, got '${OPS_DEST}'"
remote_dest="$(printf '%q' "$OPS_DEST")"

# rsync skips .git, so the server can't compute this. The same SHA tags Sentry events (API and
# worker) and the web bundle, and names the images. Uncommitted edits still get deployed, and
# the suffix says so.
RELEASE="$(git -C "$ROOT" rev-parse --short HEAD)"
if [[ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]]; then
  RELEASE="${RELEASE}-dirty"
  echo "warning: uncommitted changes will be deployed as release ${RELEASE}" >&2
fi
branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
echo "Deploying ${RELEASE} (branch ${branch}) to ${OPS_HOST}:${OPS_DEST}"

# The .env stays on the server, so check it there, before anything is copied.
remote_env_check() {
  cat << EOF
cd ${remote_dest} 2>/dev/null || exit 10
test -f .env || exit 11
if grep -v '^[[:space:]]*#' .env | grep -Eq '${PLACEHOLDER_PATTERN}'; then exit 12; fi
EOF
}
check_status=0
run ssh "$OPS_HOST" "$(remote_env_check)" || check_status=$?
case "$check_status" in
  0) ;;
  10) die "${OPS_DEST} does not exist on ${OPS_HOST}. Do the one-time setup in docs/deploy.md, step 1." ;;
  11) die "${OPS_DEST}/.env does not exist on ${OPS_HOST}. Create it from deploy/server.env.example (docs/deploy.md, step 1)." ;;
  12) die "${OPS_DEST}/.env still has a CHANGE_ME or a local value (127.0.0.1, localhost). Fix it on the server." ;;
  *) die "could not check ${OPS_DEST}/.env on ${OPS_HOST} (ssh exited ${check_status})" ;;
esac

# Patterns without a slash match at any depth. The .env* files stay out of the transfer, and rsync
# never deletes an excluded file on the receiving side, so the server's .env survives --delete.
excludes=()
for pattern in \
  .git .claude node_modules .venv '.env*' bench/images e2e/test-results playwright-report \
  __pycache__ .pytest_cache .ruff_cache .DS_Store web/dist; do
  excludes+=(--exclude "$pattern")
done
run rsync -az --delete "${excludes[@]}" "${ROOT}/" "${OPS_HOST}:${OPS_DEST}/"

# The images build on the server, which avoids cross-compiling from arm64 to amd64.
# --remove-orphans only touches containers of the ai-gallery project.
run ssh "$OPS_HOST" \
  "cd ${remote_dest} && RELEASE=$(printf '%q' "$RELEASE") docker compose -f ${COMPOSE_FILE} --env-file .env up -d --build --remove-orphans"

if [[ "$DRY_RUN" == 1 ]]; then
  echo "[dry-run] would poll ${HEALTH_URL} for up to ${HEALTH_TIMEOUT}s"
  exit 0
fi

# The first deploy can take longer than this: Traefik requests the Let's Encrypt certificate on
# the first request, and until then it serves a self-signed one that curl rejects.
echo "Waiting for ${HEALTH_URL} (up to ${HEALTH_TIMEOUT}s)"
deadline=$((SECONDS + HEALTH_TIMEOUT))
status=000
while ((SECONDS < deadline)); do
  status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL" || true)"
  if [[ "$status" == 200 ]]; then
    echo "Healthy: release ${RELEASE} is live."
    exit 0
  fi
  sleep 2
done

echo "error: ${HEALTH_URL} did not return 200 within ${HEALTH_TIMEOUT}s (last status ${status})" >&2
echo "Look: ssh ${OPS_HOST} 'cd ${OPS_DEST} && docker compose -p ai-gallery ps && docker compose -p ai-gallery logs --tail 50'" >&2
echo "Roll back: check out the previous commit and run this script again (docs/deploy.md)." >&2
exit 1
