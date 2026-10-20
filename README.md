# AI Image Gallery

Upload images, get AI-generated tags, a one-sentence description and dominant colors, then search your gallery by text, similarity or color.

**Status:** in development. The swarm board is the [MVP tracking issue](../../issues/29).
**Live:** `https://gallery.hukiewitz.com` (coming soon)

- [Architecture](docs/architecture.md): design, decisions and the API contract
- [Critical path](docs/critical-path.md): tickets, dependencies and agent models
- [AI service decision](docs/ai-service-choice.md): the options compared, the costs, and why we chose Claude
- [AGENTS.md](AGENTS.md): conventions every contributor (human or agent) follows

## Local development

You need [Docker](https://docs.docker.com/get-docker/) (for the local Supabase stack), [uv](https://docs.astral.sh/uv/) (it downloads Python 3.12 itself), Node 22 or newer, and `make`.

```bash
make dev-db                 # start local Supabase (first run downloads images); prints the URL and keys
cp .env.example .env        # then paste those values into SUPABASE_* and VITE_SUPABASE_*
(cd api && uv sync)         # Python dependencies
(cd web && npm ci)          # JavaScript dependencies
```

Then run each of these in its own terminal:

```bash
make dev-api                # http://localhost:8000/api/docs
make dev-worker             # AI analysis; ANALYZER=fake needs no key
make dev-web                # http://localhost:5173
```

`make test` runs the unit tests, `make lint` runs the linters and type checks, and `make help` lists everything else. The Supabase CLI needs no install: `make` runs a pinned version through `npx`. More detail (production setup, deployment and a walkthrough) is coming with #25.
