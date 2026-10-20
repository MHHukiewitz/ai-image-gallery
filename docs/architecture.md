# AI Image Gallery: Architecture

**Status:** Signed off by Mike, 2026-10-20. Changes go through a PR that edits this file.
**Target:** `https://gallery.hukiewitz.com` on the ops-server Hetzner box (5.75.189.90, existing Traefik + Let's Encrypt).

---

## 1. What we optimize for

The grading weights are: core functionality 35%, code quality 25%, UI/UX 20%, AI research 15%. So the design favors:

- **Few moving parts.** No Redis, no vector DB, no self-hosted Supabase.
- **One clear rule per boundary.** Each boundary below has one rule that a reviewer can check.
- **Seams where the challenge invites change.** The AI provider, the search ranking and new job kinds can each change without touching other layers.
- **Many small tickets.** Their file ownership doesn't overlap, so cheap agents can work in parallel without merge fights.

## 2. Key decisions

| # | Decision | Why |
|---|---|---|
| D1 | **Supabase Cloud** (EU/Frankfurt, free tier) for Auth, Postgres and Storage. Local dev uses `supabase start`. | The challenge requires Supabase. Self-hosting it on the 8 GB box means about 10 extra containers and earns no extra points. |
| D2 | The browser uses Supabase **only for Auth**. All data goes through **our API** (`/api/v1`). | There's one typed contract, and the UI never learns table names. The API is also the one place for validation and error mapping. |
| D3 | The API calls Supabase **with the user's JWT** and holds **no secret key**. | RLS stays the single authorization source, even on the server path. A bug in the API can't leak another tenant's data. |
| D4 | **Ingest is synchronous** in the API: validate, thumbnail, store, insert. **AI enrichment is asynchronous** in a worker. | The thumbnail appears instantly and the AI never blocks the upload. |
| D5 | **The queue is `image_metadata.ai_processing_status`**, claimed through an RPC that uses `FOR UPDATE SKIP LOCKED`. | It's durable, needs no Redis, and gives one source of truth for status. |
| D6 | **Dominant colors are computed locally** (Pillow quantize). AI is used only for tags and the sentence. | Local extraction is free and deterministic, and more accurate than hex codes guessed by an LLM. Colors still work when the AI fails. |
| D7 | An **`ImageAnalyzer` port** with three adapters: a **native Anthropic adapter** (the production path), an **OpenAI-compatible adapter** (benchmark comparisons and fallback) and a **`FakeAnalyzer`**. | Claude runs on the Max plan's monthly API credits. The official SDK gives typed structured outputs, typed errors and refusal handling. The OpenAI-compatible adapter keeps the comparison honest and keeps another vendor one config change away. Tests and offline dev need no key. |
| D8 | **Search and similarity live in Postgres SQL functions** (`security invoker`). | They run under RLS (so search is automatically per-user), paginate in the DB and need no vector DB. |
| D9 | An **AI result cache** keyed by `(content_sha256, analyzer_id)`. | Re-uploads and re-analysis with the same model cost nothing. Changing the prompt version invalidates the cache. |
| D10 | **React + Vite + TypeScript + Tailwind + TanStack Query**, served as a static SPA by nginx. | This matches the challenge stack and is cheap to host. TanStack Query handles caching, pagination and polling. |
| D11 | **Same-origin deploy:** Traefik routes `gallery.hukiewitz.com` to web and `…/api` to FastAPI. | No CORS, and the existing ACME and `security-headers@file` middleware are reused. |
| D12 | **Observability is structured JSON logs plus Sentry** (errors only, EU region, no replay, scrubbed). Sentry sends nothing without a DSN. **No Dagster, MLOps platform or product analytics.** | Per-image jobs don't fit a batch orchestrator, and the DB queue already handles retries. Sentry catches what logs miss. Replay would record users' private photos, and PostHog would add GDPR consent work without earning points. |

## 3. System view

```mermaid
flowchart LR
  subgraph Browser
    SPA[React SPA]
  end
  subgraph VPS["ops-server · Traefik + LE"]
    WEB[web · nginx static]
    API[api · FastAPI]
    WRK[worker · Python]
  end
  subgraph SUPA["Supabase Cloud · EU"]
    AUTH[Auth]
    PG[(Postgres + RLS)]
    ST[(Storage · private bucket)]
  end
  AIP[Claude API · Anthropic]

  SPA -- "gallery.hukiewitz.com" --> WEB
  SPA -- "email / password" --> AUTH
  SPA -- "/api/v1 + Bearer JWT" --> API
  API -- "as user (RLS)" --> PG
  API -- "as user (folder policies)" --> ST
  WRK -- "secret key: claim + save" --> PG
  WRK -- "download original" --> ST
  WRK -- "tags + sentence (JSON)" --> AIP
```

**Trust boundaries**

| Component | Holds | Can touch |
|---|---|---|
| Browser | Supabase URL and **publishable** key (public by design) | Auth only. Everything else goes through the API. |
| API | Publishable key and the JWKS URL | Only the requesting user's rows and objects, enforced by RLS |
| Worker | **Secret key** and **AI key** | All tenants' rows and objects, but only through the queue RPCs and by job `user_id`. It has no ingress. |

## 4. Flows

### Upload and analysis

```mermaid
sequenceDiagram
  participant B as Browser
  participant A as API
  participant S as Supabase (PG + Storage)
  participant W as Worker
  participant L as Claude API
  B->>A: POST /api/v1/images (1 file/request, XHR progress, 3 concurrent)
  A->>A: verify JWT · sniff JPEG/PNG · ≤10 MB · ≤40 MP · EXIF-orient · 300×300 WebP · sha256
  A->>S: upload {uid}/{uuid}/original.ext + thumb.webp (as user)
  A->>S: insert images + image_metadata(status=pending) (as user)
  A-->>B: 201 ImageOut (thumbnail_url, status=pending)
  loop adaptive poll 1s → 10s when idle
    W->>S: rpc claim_analysis_jobs(n)
  end
  W->>S: download original
  W->>W: dominant colors (local)
  W->>S: ai_cache lookup (sha256, analyzer_id)
  W->>L: image (≤1024px JPEG) + JSON schema prompt
  L-->>W: {tags[5..10], description}
  W->>S: upsert ai_cache · update metadata status=done
  B->>A: GET page every 2s while any visible item is pending
```

**Failure handling**

- If the DB insert fails after the objects were uploaded, the API deletes the objects.
- The adapter retries transient errors itself (429, 5xx, timeouts): 2 retries with exponential backoff. Invalid JSON gets one repair attempt.
- At the job level, `attempts < 3` sends the job back to `pending` with `next_attempt_at`. After that it becomes `failed`, with `last_error` set and the colors kept.
- A crashed worker leaves jobs in `processing`. After 5 minutes, `claim_analysis_jobs` reclaims them.
- The UI shows "Analysis failed · Retry". Retry calls `POST /images/{id}/reanalyze`, which calls the `request_reanalysis` RPC.

### Search

The URL state drives one grid:

- `/` lists all images.
- `/?q=beach` runs a text search.
- `/?similar_to=42` finds similar images.
- `/?color=%23aabbcc` filters by color.
- `&image=42` opens the modal, so the back button closes it.

Each of these maps to the single list endpoint `GET /api/v1/images` with one of the filters. The service dispatches to an SQL function, and the result comes back as `Page[ImageOut]`. The grid never knows which mode it's in.

## 5. Data model

The challenge's table and column names are kept. Columns are only added.

```sql
images(
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users on delete cascade,
  filename varchar(255) not null,            -- display only, sanitized
  original_path text not null,               -- {user_id}/{uuid}/original.jpg|png
  thumbnail_path text not null,              -- {user_id}/{uuid}/thumb.webp
  mime_type text not null, size_bytes int not null,
  width int not null, height int not null,
  content_sha256 char(64) not null,
  uploaded_at timestamptz not null default now()
)
image_metadata(
  id bigint generated always as identity primary key,
  image_id bigint not null unique references images on delete cascade,
  user_id uuid not null references auth.users on delete cascade,
  description text, tags text[] not null default '{}', colors varchar(7)[] not null default '{}',
  ai_processing_status varchar(20) not null default 'pending'
    check (ai_processing_status in ('pending','processing','done','failed')),
  attempts int not null default 0, next_attempt_at timestamptz, claimed_at timestamptz,
  last_error text, analyzer_id text,
  search_vector tsvector,                    -- trigger-maintained: tags (weight A) + description (B)
  colors_lab float8[],                       -- trigger-maintained from colors (CIE Lab, 3×3 flattened)
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
)
ai_cache(content_sha256 char(64), analyzer_id text, result jsonb not null,
         created_at timestamptz default now(), primary key (content_sha256, analyzer_id))
```

**Indexes**

- `images(user_id, uploaded_at desc, id desc)`
- GIN on `search_vector`
- GIN on `tags`
- Partial index on `image_metadata(next_attempt_at) where status = 'pending'`

**RLS policies.** All tables have RLS enabled. Policies are per command and `to authenticated`, using `(select auth.uid()) = user_id` with matching `with check`:

| Table | Policy |
|---|---|
| `images` | `select`, `insert`, `delete` for the owner |
| `image_metadata` | `select` for the owner. `insert` only with `status = 'pending'` and only for an owned image. **No `update` for users.** The worker updates through the secret key, and users go through `request_reanalysis(image_id)`, which is `security definer` and checks ownership. |
| `ai_cache` | RLS on, with **no policies**, so only the secret key can access it |

**Storage.** A private bucket named `images`. The `select`, `insert` and `delete` policies on `storage.objects` require `(storage.foldername(name))[1] = (select auth.uid())::text`.

**Queue RPCs.** These are `security definer` and `grant execute` to `service_role` only:

- `claim_analysis_jobs(n)`
- `complete_analysis_job(...)`
- `fail_analysis_job(...)`

**Search RPCs.** These are `security invoker`:

- `search_images(q, lim, off)`
- `similar_images(image_id, lim, off)`
- `images_by_color(hex, lim, off)`

## 6. API contract (v1)

All endpoints except health require `Authorization: Bearer <supabase access token>`. Errors use one shape: `{"error": {"code": "...", "message": "..."}}`.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | Liveness check. With `?deep=1` it also checks the DB. Used by Uptime Kuma. |
| POST | `/api/v1/images` | Multipart `file`. Returns 201 `ImageOut`, 413 if too large, 415 if not JPEG/PNG. |
| GET | `/api/v1/images` | `offset` (default 0) and `limit` (20, max 50). **At most one** of `q`, `similar_to` or `color` (`#rrggbb`); otherwise 422. Returns `Page[ImageOut]`. |
| GET | `/api/v1/images/{id}` | Returns `ImageOut` plus `original_url` |
| DELETE | `/api/v1/images/{id}` | Returns 204. Removes the objects and the rows. |
| POST | `/api/v1/images/{id}/reanalyze` | Returns 202. Resets the status to `pending`. |

```ts
type AnalysisStatus = "pending" | "processing" | "done" | "failed";
interface ImageOut {
  id: number; filename: string; uploaded_at: string; width: number; height: number;
  thumbnail_url: string;            // signed, 1h
  original_url?: string;            // detail endpoint only
  analysis: { status: AnalysisStatus; tags: string[]; description: string | null;
              colors: string[]; error: string | null };
  score?: number;                   // search modes only
}
interface Page<T> { items: T[]; next_offset: number | null }
```

Pydantic models in `api/src/gallery/api/schemas.py` are the source of truth. The TypeScript types are generated from OpenAPI with `openapi-typescript`, so neither side hand-writes them.

## 7. AI analysis

**Prompt.** The model gets a system prompt and a strict JSON schema:

```json
{ "tags": ["5–10 items, lowercase, 1–2 words"], "description": "one sentence, ≤ 25 words" }
```

The prompt is versioned (`PROMPT_VERSION`), and `analyzer_id = f"{provider}:{model}:{PROMPT_VERSION}"`.

**Normalization.** Tags are trimmed, lowercased and deduplicated, then clamped to 10. If fewer than 5 come back, the adapter retries once.

**Usage.** Each analysis returns `usage`: provider latency, input and output tokens, and the provider-reported cost. The worker logs it once per job, which makes the worker log our cost dashboard. The benchmark (#6) reads the same field.

**Cost control.** The image is downscaled to a 1024 px long side as JPEG q85 before sending. This uses Everlast's `normalize_image` pattern. Colors never cost tokens.

**Provider choice.** We use **Claude through the Anthropic API**. The full comparison and rationale are in the decision record, **[ai-service-choice.md](ai-service-choice.md)**. It's paid from the Max plan's monthly API credits, so this demo's volume costs nothing extra. **Mike picks the model** from #6's measured benchmark, which is the deliverable for the 15% research item.

The benchmark runs 20 fixture images through these candidates:
- **Claude:** `claude-haiku-5-5`, `claude-sonnet-5-5` and `claude-opus-5-5`, directly through the native adapter (#31);
- **Non-Claude reference:** at least one model through OpenRouter.

It records cost per 1k images, p50/p95 latency, JSON validity rate and a 1–5 quality score. The doc compares those results against classic vision APIs and self-hosting:

- **Vision LLMs (one call returns tags and a sentence):** Gemini Flash/Flash-Lite class, OpenAI mini/nano class, Claude Haiku class.
- **Classic vision APIs:** Google Cloud Vision and AWS Rekognition. They return labels and colors but no sentence, so a second service would be needed.
- **Self-hosted:** Ollama with a Qwen-VL class model on the VPS. It's free and private, but too slow on CPU.
- **Typed decision model:** TypeSafe Jev 1.14, which has experimental image input. It only returns typed answers (Choice, Score, Boolean, Box), so it can't write the sentence and can only tag from a fixed vocabulary. It's listed in the comparison but not benchmarked. Our pipeline could later use it as a cheap verifier for tags or moderation.

**Rough cost per image**, about 1.4K input tokens with a few hundred output tokens at low effort:
- Haiku 5.5: well under $0.001
- Sonnet 5.5: under $0.01
- Opus 5.5: about $0.01

All three fit easily within $200 a month for a demo. #6 replaces these estimates with measured numbers.

**Colors.** The worker downsamples to 128 px and runs `Image.quantize(colors=8, method=MEDIANCUT)`. It ranks the palette by pixel count and drops near-duplicates (ΔE < 10). Then it returns the top 3 as `#rrggbb`.

## 8. Similarity (cosine, no vector DB)

- **Tag similarity:** `tag_sim(A, B) = |A ∩ B| / sqrt(|A| · |B|)`. This is cosine similarity on binary tag vectors.
- **Color similarity:** `color_sim(Ct, Cc) = mean over c in Ct of max over d in Cc of (1 − min(ΔE76(c, d), 100) / 100)`. It's computed in CIE Lab, because RGB distance doesn't match how people perceive color. The doc will explain this.
- **"Find similar":** `score = 0.7 · tag_sim + 0.3 · color_sim`. It excludes the target and keeps `score > 0.1`, sorted descending.
- **"Filter by color":** `dist = min over d in Cc of ΔE76(query, d)`. It keeps `dist ≤ 25`, sorted ascending.
- **Text search:** `search_vector @@ query`, where the query is built from the input tokens with `:*` prefix matching on each token, so results update as you type. Results are ranked by `ts_rank_cd` plus a boost for an exact tag match.

The weights and thresholds are SQL constants at the top of the migration, each with a one-line comment.

## 9. Repo layout and layering rules

```
ai-image-gallery/
├─ api/                         Python 3.12 · uv · FastAPI · one package, two entrypoints
│  └─ src/gallery/
│     ├─ main.py                app factory (python -m uvicorn gallery.main:app)
│     ├─ worker.py              worker entrypoint (python -m gallery.worker)
│     ├─ config.py              pydantic-settings, one Settings class
│     ├─ observability.py       JSON logging + Sentry init (the only sentry_sdk import)
│     ├─ api/                   HTTP only: routes/, deps.py (auth), schemas.py, errors.py
│     ├─ services/              use cases: uploads.py, analysis.py, gallery.py
│     ├─ domain/                pure, no I/O: models.py, colors.py, thumbnails.py, validation.py
│     ├─ ports.py               Protocols: ImageRepo, ObjectStore, ImageAnalyzer, JobQueue
│     └─ adapters/              supabase_client.py, supabase_images.py, supabase_storage.py,
│                                supabase_jobs.py, supabase_cache.py, analyzers/{anthropic,openai_compat,fake}.py
├─ web/                         React · Vite · TS · Tailwind · TanStack Query
│  └─ src/
│     ├─ app/                   router, providers, layout
│     ├─ lib/                   supabase.ts (auth only), api.ts (fetch + XHR upload), api-types.ts (generated),
│     │                         observability.tsx (Sentry init + error boundary; the only @sentry/react import)
│     ├─ features/              auth/ gallery/ upload/ search/ image-detail/
│     ├─ components/ui/         Button, Input, Skeleton, Dialog, Toast …
│     └─ mocks/                 MSW handlers from contract fixtures (web dev without backend)
├─ supabase/                    config.toml · migrations/ · seed.sql · tests/ (pgTAP)
├─ deploy/                      compose.prod.yml (Traefik labels)
├─ scripts/                     deploy.sh · bench_analyzers.py · gen-api-types.sh
├─ docs/                        architecture.md · ai-service-choice.md · critical-path.md · adr/
├─ docker-compose.yml           local api + worker + web (Supabase via `supabase start`)
├─ AGENTS.md                    conventions every swarm agent must follow
└─ .github/workflows/ci.yml
```

**Rules that reviews enforce:**

1. **Import direction:** `api → services → ports ← adapters`, and `domain` imports nothing from the app. Routes contain no business logic, and services don't import FastAPI.
2. **Web:** feature folders own their components, hooks and queries. Cross-feature imports go only through `lib/` or `components/ui/`. Supabase is imported in `lib/supabase.ts` and `features/auth` and nowhere else.
3. **Config:** every setting lives in `Settings` or `import.meta.env`, and each one is documented in `.env.example`. There are no literals for URLs or keys.
4. **Tests ship with the code:** domain and service changes need unit tests, and migrations need a pgTAP or integration test.
5. **Commits:** Conventional Commits (`feat(api): …`). Each ticket gets its own branch `t{issue}-{slug}` and a PR that closes the issue. Rebase-merge keeps the commit history readable, which is graded.

## 10. Deployment (ops-server)

- **Compose project:** `deploy/compose.prod.yml` is its own project. Its `api`, `worker` and `web` services join the external `sites` network that Traefik already watches. The worker joins only the default network.
- **Images:** `api` and `worker` share one image and differ only by command. `web` is a multi-stage build: `vite build` with `VITE_*` build args, then `nginx:1.27-alpine`.
- **Traefik labels:** `Host(gallery.hukiewitz.com)` routes to web:80, and `Host(...) && PathPrefix(/api)` routes to api:8000. Both use `certresolver=le` and `security-headers@file`, the same pattern as `site-hukiewitz`.
- **Deploy:** `scripts/deploy.sh` rsyncs the repo to `/opt/ai-gallery` (skipping `.env`, `node_modules` and `.venv`), then runs `ssh … docker compose -f deploy/compose.prod.yml up -d --build`. Building on the box avoids cross-compiling from arm64 to amd64. `.env` exists only on the server.
- **Migrations:** `supabase db push` from the laptop (or CI later). They're never run from containers.
- **Keeping Supabase awake:** free-tier Supabase pauses after a week of inactivity. The worker's polling plus an Uptime Kuma check on `/api/health?deep=1` should keep it awake. Otherwise, use Pro for the review window.
- **Footprint:** about 300 MB of RAM in total.

## 11. Testing and CI

| Layer | What is tested | Tooling |
|---|---|---|
| Domain | colors, thumbnails, validation, tag normalization | pytest, fixture images |
| Services | uploads and analysis against fake ports (including AI failure and cache hit) | pytest |
| DB | **RLS isolation**: user B can't read, search, find similar to, or delete A's images. The search RPCs return the expected ordering. | pgTAP or pytest against `supabase start` |
| Web | the query-state ↔ URL mapping and the upload queue reducer | Vitest + Testing Library |
| End to end | sign up → upload 2 → analyzed → text, similar and color search → logout | Playwright against the local stack with `FakeAnalyzer` |
| CI | ruff, pytest, tsc, eslint, vitest, docker build, gitleaks. A Supabase integration job runs via `supabase/setup-cli`. | GitHub Actions |

### Observability

| Concern | Approach |
|---|---|
| **Logs** | One JSON object per line on stdout from the API and the worker. `docker compose logs` is enough. The worker writes one line per job with the outcome, latency, cache hit, tokens and `cost_usd`. |
| **Errors** | Sentry, in the EU region and free tier. There's one Python project, with the API and worker told apart by a `component` tag, and one React project. |
| **How errors reach Sentry** | Through log levels. Sentry's logging integration sends `ERROR` and above, so only `observability.py` and `lib/observability.tsx` import Sentry. |
| **Privacy** | `send_default_pii` is off. Request bodies are never sent (`max_request_body_size="never"`), so upload bytes stay out. The `Authorization` and `Cookie` headers are dropped, and `token=` values in signed image URLs are redacted. There's no session replay and no tracing. |
| **Uptime** | Uptime Kuma (already on ops-server) checks `/api/health?deep=1`. |

**Deliberately not used:**
- **Dagster or another orchestrator:** per-image, latency-sensitive jobs don't fit a batch orchestrator, and re-analyzing everything is one `update … set status = 'pending'`.
- **MLflow / W&B:** we train nothing, and `analyzer_id` on each row plus the benchmark record which model and prompt produced what.
- **Langfuse:** its traces would carry users' photos.
- **PostHog:** it needs GDPR consent and adds no value for grading.

**GlitchTip** can replace Sentry by swapping the DSN.

## 12. Reuse from Everlast-NotebookLLM

That project's stack is Next.js + FastAPI + SQLAlchemy/pgvector + arq/Redis + its own bcrypt sessions + litellm + Caddy. **We don't fork it.** Supabase replaces its auth, DB and storage layers, and the challenge stack differs everywhere else. Adapting it would cost more than writing fresh.

**Worth taking:**

- `normalize_image`, `image_data_url` and `vision_messages` from `apps/api/app/services/ingest.py`, about 40 lines, which go into the OpenAI-compatible adapter.
- The **existing OpenRouter key** for the benchmark's non-Claude reference model, so no new account is needed. Claude itself runs on the Max plan's API credits.

**Not reused:**

- **Auth and sessions:** Supabase Auth replaces them.
- **SQLAlchemy models:** our schema is small and lives in SQL migrations.
- **arq/Redis:** the DB queue replaces it.
- **Next.js:** a Vite SPA is lighter.
- **litellm:** it's a heavy dependency for one call. We use the official `anthropic` SDK for Claude, and the `openai` SDK with a `base_url` for the comparison.
- **Caddy:** we use Traefik.

The useful deploy reuse comes from **ops-server**, not Everlast: the Traefik label pattern, the `sites` network, `security-headers@file`, the rsync deploy and Uptime Kuma.

## 13. Swarm plan

The ticket breakdown, dependencies, agent models and the critical-path diagram live in **[critical-path.md](critical-path.md)**. Each ticket is a GitHub issue that lists the paths it owns. The workflow rules every agent follows are in **[AGENTS.md](../AGENTS.md)**.

## 14. Decisions signed off (2026-10-20)

| Question | Decision |
|---|---|
| Subdomain | `gallery.hukiewitz.com`, on ops-server (5.75.189.90) behind the existing Traefik |
| Supabase | Supabase Cloud, free tier, EU (Frankfurt). Local dev uses `supabase start`. |
| Email confirmation | **Off**, so reviewers can sign up instantly. This is documented in the README. Resend SMTP can be added later. |
| Repo | `MHHukiewitz/ai-image-gallery`, **public**. The local checkout is `~/Workspace/ai-image-gallery`. |
| AI provider | Claude through the Anthropic API, paid from the Max plan's monthly API credits, with a dedicated key and a spend-limited workspace (#28). Mike picks the model from #6's benchmark of Haiku 5.5, Sonnet 5.5 and Opus 5.5. |
| Observability | JSON logs and Sentry for errors, using Sentry's EU region (optional, with #30). No Dagster, MLOps platform, Langfuse or PostHog. |
| Jev (TypeSafe) | Not in the pipeline. It's typed-answer only and image input is experimental. It's listed as a rejected option in the research doc. |
