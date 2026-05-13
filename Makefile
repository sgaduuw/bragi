.PHONY: help install dev test test-fast lint fmt typecheck migrate revision

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install all dependencies via poetry
	poetry install

dev:  ## Run admin + delivery side by side (honcho)
	poetry run honcho -f Procfile.dev start

test:  ## Run the full test suite
	poetry run pytest

test-fast:  ## Run only unit tests (no DB, no app)
	poetry run pytest tests/unit

lint:  ## Lint and format-check
	poetry run ruff check src/ tests/
	poetry run ruff format --check src/ tests/

fmt:  ## Auto-fix lint and apply formatting
	poetry run ruff check --fix src/ tests/
	poetry run ruff format src/ tests/

typecheck:  ## Run mypy
	poetry run mypy src/

migrate:  ## Apply migrations to the local DB
	poetry run alembic upgrade head

revision:  ## Generate a new migration: make revision m="describe"
	poetry run alembic revision --autogenerate -m "$(m)"
