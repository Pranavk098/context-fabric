.PHONY: help sync fmt lint typecheck test det spike

help:
	@echo "sync      - create/refresh the uv environment (Python 3.12)"
	@echo "fmt       - ruff format"
	@echo "lint      - ruff check"
	@echo "typecheck - mypy (strict)"
	@echo "test      - pytest"
	@echo "det       - zero-GPU deterministic-core spike (Stage B check 1)"
	@echo "spike     - KV-cache spike against CF_BASE_URL (Stage B check 2)"

sync:
	uv sync --python 3.12

fmt:
	uv run ruff format .

lint:
	uv run ruff check .

typecheck:
	uv run mypy protocol evals

test:
	uv run pytest -q

det:
	uv run python -m evals.spike_deterministic

spike:
	uv run python -m evals.spike_kv_cache
