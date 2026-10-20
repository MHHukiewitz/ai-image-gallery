# AGENTS.md: rules for every contributor (agent or human)

These rules apply to every ticket. If your issue contradicts them, follow the issue, and say so in the PR description.

## Before you start

1. Read your issue completely, then **docs/architecture.md** (at least the sections your issue cites), then this file.
2. Check that every issue listed under **Depends on** is closed. If one is open, stop and report that the ticket is blocked.
3. Branch from the latest `main` and name it `t<issue>-<short-slug>`, for example `t8-upload-ingest`.

## Scope

- **Edit only the paths listed under "Owned paths" in your issue.** You may read anything.
- Need a change outside your paths, such as a missing port method or a wrong contract? **Don't make it.** Describe it under "Blockers / follow-ups" in your PR. The orchestrator decides.
- **Don't add dependencies.** Ticket #1 pinned all runtime and dev dependencies. If you truly need one, add it as a separate commit (`build(deps): …`) and explain why in the PR.
- Don't refactor code you don't own, even when it's tempting.

## Layering (enforced in review)

**API side** (`api/src/gallery/`):

- `api/` is the HTTP layer: routes, dependencies, schemas and error mapping. **No business logic.**
- `services/` holds the use cases. They depend only on `ports.py` Protocols and `domain/`. **They never import FastAPI or supabase.**
- `domain/` is pure: no I/O, no framework imports. It's easy to unit-test.
- `adapters/` implements the ports with Supabase or AI SDKs. Nothing outside `adapters/` imports `supabase`, `anthropic` or `openai`.
- The API never uses the Supabase secret key. Only the worker does, through `adapters/supabase_client.service_client`.

**Web side** (`web/src/`):

- `features/<name>/` owns its components, hooks and tests. **A feature never imports another feature.**
- `app/` is the only place that composes features: router, layout and the gallery route.
- Shared code lives in `lib/` (API client, query keys, gallery URL state, the Supabase auth client) and `components/ui/` (primitives).
- Only `lib/supabase.ts` and `features/auth/` may import `@supabase/supabase-js`. All data goes through `lib/api.ts`.
- Only `lib/observability.tsx` imports `@sentry/react`. On the API side, only `observability.py` imports `sentry_sdk`.

## Code style

- **Python:**
  - Use 3.12 with type hints on every function.
  - Run `ruff format` and `ruff check` before committing.
  - Use dataclasses or Pydantic, not dicts, across layer boundaries.
  - Log with `logging.getLogger(__name__)`. Put structured fields in `extra={...}`. No `print`.
  - Log levels matter: `ERROR` and above reach Sentry. Use `logger.exception` for bugs, `warning` for retryable conditions, and `info` for user mistakes.
- **TypeScript:**
  - `strict` mode, no `any`, function components, named exports.
  - Style with Tailwind utility classes. The only CSS file is `src/index.css`.
  - Format with Prettier.
- **Names** say what the thing is (`thumbnail_path`, not `p2`). Avoid abbreviations except well-known ones (`id`, `url`, `db`).
- **Comments** explain *why*, or a non-obvious constraint. Don't narrate the code. Put a short docstring on each public function whose behavior isn't obvious from its name and types.
- **Errors:** raise domain errors (`domain/errors.py`) in services. `api/errors.py` maps them to HTTP. Users see one error shape: `{"error": {"code", "message"}}`.

## Tests

- Every behavior change ships with tests in the same PR.
  - `domain/` and `services/` changes get unit tests with fakes (no network).
  - Migrations get pgTAP tests in `supabase/tests/`.
  - Web logic gets Vitest tests.
- Tests needing the local Supabase stack are marked `@pytest.mark.integration`.
- Never call a real AI provider in tests. Use `FakeAnalyzer` or an HTTP mock.
- Claude code follows the `claude-api` skill: load it before touching `adapters/analyzers/anthropic.py`, and use the official `anthropic` SDK, never an OpenAI-compatible shim.

## Secrets

- Never commit `.env` files, keys or tokens. Use `.env.example` with placeholder values.
- Tests use obviously fake values (`test-key`). Never write a real key into an issue, a PR or a log line.
- The browser only gets `VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY`. Both are public by design.

## Commits and PRs

- Use small, focused commits in **Conventional Commits** form: `feat(api): add upload ingest service`, `test(db): cover RLS isolation`, `fix(web): …`. The history is graded, so make each commit a step that tells the story.
- End every commit message with:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` for orchestrator commits, or your own model's equivalent line.
- Open one PR per issue using the template, with `Closes #<issue>` in the body.
- **Don't merge.** The orchestrator reviews and rebase-merges.
- CI must be green. If a failure is outside your scope, explain it in the PR instead of patching around it.

## Definition of done

- [ ] Every acceptance criterion in the issue is checked off and shown in "How I verified it"
- [ ] The tests and lint you're responsible for pass locally (see Commands below)
- [ ] There are no TODOs without a linked issue
- [ ] The PR touches only owned paths, or explains each exception

## Commands

_Ticket #1 fills this section in: dev servers, tests, lint, formatting, type generation and DB tests._
