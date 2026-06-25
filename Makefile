.PHONY: help install dev test test-fast lint fmt typecheck migrate revision

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install all dependencies via uv
	uv sync

dev: migrate  ## Apply migrations, then run admin + delivery side by side
	# BRAGI_DELIVERY_BASE_URL points admin->delivery links (the working-copy
	# preview) at the local delivery port, which the per-site canonical_url
	# can't express in dev. Matches the delivery port in Procfile.dev.
	BRAGI_DELIVERY_BASE_URL=http://localhost:8002 uv run python scripts/run-procfile.py Procfile.dev

test:  ## Run the full test suite
	uv run pytest

test-fast:  ## Run only unit tests (no DB, no app)
	uv run pytest tests/unit

lint:  ## Lint and format-check
	uv run ruff check src/ tests/ alembic/
	uv run ruff format --check src/ tests/ alembic/

fmt:  ## Auto-fix lint and apply formatting
	uv run ruff check --fix src/ tests/ alembic/
	uv run ruff format src/ tests/ alembic/

typecheck:  ## Run mypy
	uv run mypy src/

migrate:  ## Apply migrations to the local DB
	uv run alembic upgrade head

revision:  ## Generate a new migration: make revision m="describe"
	uv run alembic revision --autogenerate -m "$(m)"
