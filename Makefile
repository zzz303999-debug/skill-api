.PHONY: install dev run docker-build docker-up docker-down docker-logs test test-golden test-mineru-golden lint smoke

install:
	uv venv --python 3.11 && uv pip install -e ".[dev]"

dev:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 9000 --reload

run:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 9000

docker-build:
	docker build -t skill-api:latest .

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

test:
	uv run pytest -q

test-golden:
	RUN_LLM_GOLDEN=1 uv run pytest -q -m golden

test-mineru-golden:
	RUN_MINERU_GOLDEN=1 uv run pytest -q -m mineru_golden

lint:
	uv run ruff check app
