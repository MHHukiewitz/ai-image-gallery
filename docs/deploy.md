# Deploying to production

The gallery runs at `https://gallery.hukiewitz.com` on ops-server (`ops@5.75.189.90`), next to the other sites on that box. Traefik already terminates TLS there. This stack only adds three containers and two routers:

| Container | Image | Reached by | Memory limit |
|---|---|---|---|
| `web` | nginx serving the built SPA, on port 8080 | Traefik, `Host(gallery.hukiewitz.com)` | 64 MB |
| `api` | FastAPI on port 8000 | Traefik, same host plus `PathPrefix(/api)`, priority 100 | 768 MB |
| `worker` | same code as `api`, runs the analysis loop | nothing: it only calls Supabase and the AI provider | 1 GB |

The database, auth and file storage live in **Supabase Cloud**, not on the server. The design is in [architecture.md](architecture.md#10-deployment-ops-server).

Everything is defined by [`deploy/compose.prod.yml`](../deploy/compose.prod.yml), a compose project named `ai-gallery` that is separate from the ops-server project. [`scripts/deploy.sh`](../scripts/deploy.sh) copies the repo to `/opt/ai-gallery`, builds the images **on the server** (so nothing is cross-compiled from arm64 to amd64) and waits for the health check.

**Going live needs the repo owner's explicit OK in issue #22.** Steps 1 to 3 are one-time setup. Step 4 is the deploy itself.

## 1. One-time server setup

From the repo root:

```bash
ssh ops@5.75.189.90 'sudo mkdir -p /opt/ai-gallery && sudo chown ops:ops /opt/ai-gallery'
scp deploy/server.env.example ops@5.75.189.90:/opt/ai-gallery/.env
ssh -t ops@5.75.189.90 'chmod 600 /opt/ai-gallery/.env && nano /opt/ai-gallery/.env'
```

The `.env` exists **only on the server**. `deploy.sh` never copies it, and never deletes it either. Start from [`deploy/server.env.example`](../deploy/server.env.example), not from the root `.env.example`: the root file holds local-development values (`127.0.0.1`, `ANALYZER=fake`). Replace every `CHANGE_ME`. The deploy script refuses to run while one is left, or while any value still points at `localhost` or `127.0.0.1`.

Also check that Traefik's `sites` network exists, because compose attaches to it and doesn't create it:

```bash
ssh ops@5.75.189.90 'docker network ls --filter name=^sites$ --format "{{.Name}}"'   # prints: sites
```

### Variables in the server `.env`

| Variable | Needed | What to put there |
|---|---|---|
| `SUPABASE_URL` | yes | `https://<project-ref>.supabase.co` |
| `SUPABASE_PUBLISHABLE_KEY` | yes | the project's publishable key (public by design) |
| `SUPABASE_SECRET_KEY` | yes | the project's secret key. Only the worker receives it. |
| `VITE_SUPABASE_URL` | yes | the same value as `SUPABASE_URL`. The browser bundle bakes it in at build time. |
| `VITE_SUPABASE_PUBLISHABLE_KEY` | yes | the same value as `SUPABASE_PUBLISHABLE_KEY` |
| `ANALYZER` | yes | `openai_compat`, Claude through OpenRouter. See "Choosing the AI provider" below. |
| `AI_API_KEY` | with `openai_compat` | the OpenRouter key. Give it a credit limit, so a bug can't drain the account. |
| `ANTHROPIC_API_KEY` | with `anthropic` | an Anthropic API key, only for the native adapter |
| `AI_MODEL` | yes | the model picked by the benchmark in #6: `anthropic/claude-haiku-5.5` on OpenRouter, `claude-haiku-5-5` with `anthropic` |
| `AI_EFFORT` | no | `low` (the default), `medium` or `high`. Only the `anthropic` adapter sends it. |
| `SENTRY_DSN` | no | the API and worker DSN from #30. Empty keeps Sentry off. |
| `VITE_SENTRY_DSN` | no | the browser DSN from #30. Empty keeps Sentry off. |

Rules that follow from the design:

- **No JWT secret in production.** Supabase Cloud signs user tokens with ES256, and the API checks them against the project's JWKS. `SUPABASE_JWT_SECRET` is never passed to a container, even if it is in the file.
- **`RELEASE` is not set here.** `deploy.sh` exports the git SHA for each deploy. `SENTRY_ENVIRONMENT` and `VITE_SENTRY_ENVIRONMENT` are fixed to `production` in the compose file, and so are `VITE_USE_MOCKS=false` and `VITE_API_BASE=/api`.
- **Anything in `.env.example` that isn't listed above is optional tuning**, such as `LOG_LEVEL`, `WORKER_CONCURRENCY`, `MAX_UPLOAD_MB` and `AI_TIMEOUT_SECONDS`. When a line is absent or empty, the default in `api/src/gallery/config.py` applies. If you raise `MAX_UPLOAD_MB`, raise `VITE_MAX_UPLOAD_MB` to match.
- **`VITE_*` values are compiled into the bundle.** Changing one means a redeploy. It rebuilds the web image.

### Choosing the AI provider

The worker reads `ANALYZER` from `.env`, so switching providers needs no code change. Production uses the first:

- `ANALYZER=openai_compat` with `AI_API_KEY`, `AI_MODEL` and optionally `AI_BASE_URL` (the default is OpenRouter). Empty `ANTHROPIC_API_KEY=`.
- `ANALYZER=fake` returns deterministic placeholder analyses. Use it for a smoke test only, never for the review.

For the native adapter instead, set `ANALYZER=anthropic`, `ANTHROPIC_API_KEY` and `AI_MODEL`, then run `scripts/deploy.sh` again. If the worker's settings are wrong, it stops at startup with a clear message (`ANALYZER=anthropic needs ANTHROPIC_API_KEY`) and Docker keeps restarting it. Watch `docker compose -p ai-gallery logs worker`.

## 2. Supabase production setup

### Migrations

Migrations are **not** run from this repo's scripts or containers. Supabase's GitHub integration applies every new file in `supabase/migrations/` to the production database when a PR merges to `main`. The integration is configured once, in the dashboard under **Project Settings → Integrations → GitHub**:

- repository `MHHukiewitz/ai-image-gallery`
- working directory `.` (the folder that contains `supabase/`)
- production branch `main`
- **Deploy to production: on**

Before every go-live, check that production has every migration. Open **Database → Migrations** in the dashboard and compare with the files:

```bash
ls supabase/migrations/
```

Each filename's leading timestamp must appear in the dashboard list. If one is missing, **don't deploy**: the integration's run on the GitHub side (a check on the merge commit) shows why. Never edit a migration that is already merged. Add a new one ([AGENTS.md](../AGENTS.md#database-migrations)).

The private `images` storage bucket is created by a migration too. **Storage** in the dashboard should list it.

### Auth

`supabase/config.toml` only configures the local stack. Production auth is set once in the dashboard:

- **Authentication → Sign In / Providers → Email**: turn **Confirm email off**, so reviewers can sign up and use the app at once (architecture §14).
- **Authentication → URL Configuration**:
  - Site URL: `https://gallery.hukiewitz.com`
  - Redirect URLs: `https://gallery.hukiewitz.com/**` and `http://localhost:5173/**`

### Keys and signing

- **Project Settings → API Keys** has the publishable and secret keys for the server `.env`. The project URL is `https://<project-ref>.supabase.co`.
- Confirm that the project signs with an asymmetric key. The API needs this, because it has no JWT secret:

  ```bash
  curl -s https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json | jq '.keys[].alg'   # "ES256"
  ```

## 3. DNS check

Traefik requests the Let's Encrypt certificate on the first request for the host, and the HTTP challenge only succeeds if DNS already points at the server. Check it before the first deploy:

```bash
dig +short gallery.hukiewitz.com   # must print 5.75.189.90
```

Repeated failed challenges run into Let's Encrypt's rate limits, so fix DNS first, then deploy.

## 4. Deploying

Deploy from a clean checkout of the commit you mean to ship, normally `main` after CI is green:

```bash
git switch main && git pull --ff-only
DRY_RUN=1 scripts/deploy.sh     # prints every command, touches nothing
scripts/deploy.sh
```

The script:

1. Computes `RELEASE` from the local git SHA (with `-dirty` added when tracked files have uncommitted changes). The server has no `.git`, so it can't do this itself.
2. Checks over ssh that `/opt/ai-gallery/.env` exists and has no `CHANGE_ME` or local value.
3. Runs `rsync --delete` into `/opt/ai-gallery/`. It skips `.git`, `node_modules`, `.venv`, every `.env*`, `bench/images`, `e2e/test-results`, `playwright-report` and caches. The server's `.env` survives because it is excluded. Keep nothing else there: files that aren't in the repo are deleted.
4. Runs `RELEASE=<sha> docker compose -f deploy/compose.prod.yml --env-file .env up -d --build --remove-orphans` on the server. Only the `ai-gallery` project is touched. Old containers are replaced once the new images are built.
5. Polls `https://gallery.hukiewitz.com/api/health?deep=1` until it returns 200, and gives up after 60 seconds.

`OPS_HOST` (default `ops@5.75.189.90`), `OPS_DEST` (`/opt/ai-gallery`), `HEALTH_URL` and `HEALTH_TIMEOUT` can be overridden from the environment.

**The first deploy is slower.** The images build from scratch, and Traefik issues the certificate on the first request. If the script gives up while the build or the certificate is still in progress, nothing is broken: run it again, or run it with `HEALTH_TIMEOUT=180`.

## 5. Smoke checks

```bash
# The SPA, over a valid Let's Encrypt certificate
curl -sI https://gallery.hukiewitz.com | head -1                          # HTTP/2 200
echo | openssl s_client -connect gallery.hukiewitz.com:443 -servername gallery.hukiewitz.com 2>/dev/null \
  | openssl x509 -noout -issuer -dates                                    # issuer: Let's Encrypt

# The API and the database behind it
curl -s 'https://gallery.hukiewitz.com/api/health?deep=1'                 # {"status":"ok","db":"ok"}

# The other sites on the box are unaffected
curl -sI https://hukiewitz.com | head -1                                  # HTTP/2 200
curl -sI https://robotter.ai | head -1                                    # HTTP/2 200

# All three containers are up. api and web show "healthy".
ssh ops@5.75.189.90 'docker compose -p ai-gallery ps'
```

Then, in a browser, on `https://gallery.hukiewitz.com`:

1. Sign up with a new email. You land in the gallery with no confirmation step.
2. Upload an image. It shows up in the gallery right away, still pending.
3. Within a few seconds the image gets its tags, description and colors. This proves the worker, the AI provider and the database work together.
4. Reload the page, then sign out and back in. The image is still there.

Record the results in issue #22.

## 6. Rollback

Deploy the previous commit. The earlier release's layers are usually still in the server's build cache, so this is quick:

```bash
git switch --detach <previous-sha>     # find it with: git log --oneline main
scripts/deploy.sh
git switch main
```

Check `RELEASE` in the script's first line of output: it should name the old SHA.

Migrations only go forward, and the app must keep working both before and after each one applies ([AGENTS.md](../AGENTS.md#database-migrations)), so an app rollback never needs a database rollback. To undo a migration, add a new one.

To take the site offline without deleting anything, stop only this project:

```bash
ssh ops@5.75.189.90 'docker compose -p ai-gallery stop'
```

Start it again with `docker compose -p ai-gallery start`, or deploy. **Never run `docker compose down` or prune a network in `/opt/ops-server`**: it holds every other site and Traefik. Every command for this stack uses `-p ai-gallery`.

## 7. Uptime Kuma (optional)

If the ops-server `apps` profile is running (`docker compose --profile apps up -d` in `/opt/ops-server`), add an HTTP monitor in Uptime Kuma:

- URL `https://gallery.hukiewitz.com/api/health?deep=1`
- accepted status codes `200-299`
- interval 5 minutes

The deep check queries the database, so it also keeps a free-tier Supabase project from pausing after a week of inactivity. If the monitor goes red, check `docker compose -p ai-gallery ps` first, then Supabase's status.

## 8. Logs

The API and the worker write one JSON object per line. Follow the worker, with a line per job that carries its outcome, latency and AI cost:

```bash
ssh ops@5.75.189.90 'cd /opt/ai-gallery && docker compose -p ai-gallery logs -f --no-log-prefix worker' | jq
```

`--no-log-prefix` keeps compose from putting `worker-1  |` in front of each line, which `jq` can't parse. Each line has `ts`, `level`, `logger`, `msg` and `component`, plus any fields the code adds. To see only problems:

```bash
ssh ops@5.75.189.90 'cd /opt/ai-gallery && docker compose -p ai-gallery logs --no-log-prefix api worker' \
  | jq -c 'select(.level == "ERROR" or .level == "WARNING")'
```

Each container keeps at most 3 files of 10 MB. Traefik's access log, which shows requests that never reach the app, is in `docker logs traefik`.

## 9. Sentry (optional, if #30 is done)

Put the DSNs into the server `.env` (`SENTRY_DSN` for the API and worker, `VITE_SENTRY_DSN` for the browser), then deploy. Events carry `environment: production`, the deployed git SHA as `release`, and a `component` tag (`api` or `worker`). In Sentry (EU region), filter **Issues** by `release:<sha>`.

Send a test event from the running API container:

```bash
ssh ops@5.75.189.90 'cd /opt/ai-gallery && docker compose -p ai-gallery exec -T api python -c "
import logging
from gallery.config import get_settings
from gallery.observability import init_observability
init_observability(get_settings(), \"api\")
logging.getLogger(\"deploy.check\").error(\"sentry test event from the deploy runbook\")
"'
```

An error-level log record is an event, and the SDK sends it before the process exits. It should show up in Sentry within a minute, tagged with the deployed `release`. With an empty `SENTRY_DSN` the command sends nothing.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `404 page not found` from Traefik on `gallery.hukiewitz.com` | Traefik routes only to healthy containers. Run `docker compose -p ai-gallery ps`, then read the unhealthy container's logs. |
| Browser warns about the certificate | The certificate isn't issued yet or the challenge failed. Check DNS (step 3), then `docker logs traefik 2>&1 \| grep -i gallery`. |
| `/api/...` returns 502 | `api` is down or restarting. Read `docker compose -p ai-gallery logs api`. |
| The deep health check returns 503 | The API can't reach Supabase. Check the project isn't paused and `SUPABASE_URL` is right. |
| Uploads stay at "analyzing" | The worker is crash-looping or failing jobs. Read its logs. Most likely `ANTHROPIC_API_KEY` or `AI_MODEL` is empty. |
| Sign-in works in the app but every API call returns 401 | The API can't verify the token. Check the JWKS URL from step 2, and that `SUPABASE_URL` matches the project that issued it. |
| The app talks to the wrong Supabase project | The `VITE_*` values are compiled into the bundle. Fix `.env`, then run `scripts/deploy.sh` again. |
| Disk fills up over time | Every release leaves images behind. `ssh ops@5.75.189.90 'docker image ls "ai-gallery-*"'` lists them. Remove old tags with `docker rmi`, or run `docker image prune -f` for dangling layers only. |
