# Developer entrypoints. `make help` lists them.
# The Supabase CLI runs through npx at a pinned version; override with
# `make SUPABASE=supabase dev-db` to use one you installed yourself.
SUPABASE ?= npx --yes supabase@2.121.0

.DEFAULT_GOAL := help
.PHONY: help dev-db dev-api dev-worker dev-web test lint fmt types db-test

help: ## Show this list
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

dev-db: ## Start the local Supabase stack (needs Docker)
	$(SUPABASE) start

dev-api: ## API on http://localhost:8000 with auto-reload
	cd api && uv run uvicorn gallery.main:app --reload --reload-dir src --port 8000

dev-worker: ## Analysis worker
	cd api && uv run python -m gallery.worker

dev-web: ## Web app on http://localhost:5173 (proxies /api to :8000)
	cd web && npm run dev

test: ## Unit tests for api and web (integration tests excluded)
	cd api && uv run pytest -m "not integration"
	cd web && npm run test -- --run

lint: ## Lint, type-check and format-check api and web
	cd api && uv run ruff check . && uv run ruff format --check .
	cd web && npm run lint && npm run typecheck && npm run format:check

fmt: ## Auto-fix lint issues and format api and web
	cd api && uv run ruff check --fix . && uv run ruff format .
	cd web && npm run format

types: ## Regenerate web/src/lib/api-types.ts from the API's OpenAPI schema
	@test -x scripts/gen-api-types.sh || { echo "scripts/gen-api-types.sh does not exist yet (ticket #2 creates it)"; exit 1; }
	./scripts/gen-api-types.sh

db-test: ## Run the pgTAP tests (needs `make dev-db` running)
	$(SUPABASE) test db
