.PHONY: install dev run docker-build docker-up docker-down docker-logs test lint smoke

install:
	uv venv --python 3.11 && uv pip install -e ".[dev]"

dev:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

run:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8080

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

lint:
	uv run ruff check app
