VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.PHONY: help install review test lint fmt eval eval-baseline serve worker dlq up down clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

$(VENV):
	python3 -m venv $(VENV) || uv venv $(VENV)

install: $(VENV) ## Install dependencies into .venv
	$(PIP) install -q -r requirements-dev.txt || uv pip install -q --python $(PY) -r requirements-dev.txt

review: ## Review a PR: make review URL=https://github.com/o/r/pull/1
	@test -n "$(URL)" || (echo "usage: make review URL=<pr-url> [POST=1]"; exit 1)
	$(PY) -m app.cli $(URL) $(if $(POST),--post,) $(ARGS)

test: ## Run the test suite
	$(PY) -m pytest -q

lint: ## Lint the codebase
	$(VENV)/bin/ruff check app tests
	$(VENV)/bin/ruff format --check app tests

fmt: ## Auto-format
	$(VENV)/bin/ruff check --fix app tests
	$(VENV)/bin/ruff format app tests

eval: ## Run the offline eval suite
	$(PY) -m app.evals --fixtures-only --verbose

eval-baseline: ## Record the current run as the baseline to compare against
	$(PY) -m app.evals --fixtures-only --out evals/baseline.json

eval-diff: ## Compare this prompt against the recorded baseline
	$(PY) -m app.evals --fixtures-only --baseline evals/baseline.json

serve: ## Run the webhook receiver locally
	$(PY) -m uvicorn app.server.app:create_app --factory --reload --port 8000

worker: ## Run the async review worker
	$(PY) -m app.server.worker

dlq: ## Show the dead-letter queue (make dlq ARGS=--requeue to drain it)
	$(PY) -m app.server.dlq $(ARGS)

up: ## Bring up redis + api + worker
	docker compose up --build

down:
	docker compose down -v

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ *.egg-info
