.DEFAULT_GOAL := help
.PHONY: help install dev run test test-integration cov lint fmt typecheck check migrate revision up down logs loadtest clean

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install deps + git hooks
	uv sync
	uv run pre-commit install

dev: ## Run with auto-reload (single worker)
	APP_LOG_JSON=false uv run uvicorn app.main:app --reload --loop uvloop --http httptools --no-access-log

run: ## Run production server locally (multi-worker)
	uv run serve

test: ## Run unit tests (+ integration tests if docker services are up)
	uv run pytest

test-integration: ## Integration tests vs real postgres/redis/clickhouse
	docker compose up -d postgres redis clickhouse
	uv run pytest -m integration

cov: ## Run tests with coverage
	uv run pytest --cov --cov-report=term-missing

lint: ## Lint
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Auto-format + autofix
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## mypy --strict + pyright (deprecations)
	uv run mypy
	uv run basedpyright

check: lint typecheck test ## Everything CI runs

migrate: ## Apply migrations
	uv run alembic upgrade head

revision: ## New migration: make revision m="add users"
	uv run alembic revision --autogenerate -m "$(m)"

up: ## Start full stack (api, postgres, redis)
	docker compose up --build -d

down: ## Stop stack
	docker compose down

logs: ## Tail API logs
	docker compose logs -f api

loadtest: ## Locust web UI on :8089
	uv run locust -f loadtest/locustfile.py --host http://localhost:8000

clean: ## Remove caches
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
