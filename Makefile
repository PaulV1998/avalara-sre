# Build, test and packaging workflow.
# Requires only python3 (>= 3.11) and make; dev tools are installed into .venv from pinned versions.

PYTHON   ?= python3
VENV     := .venv
BIN      := $(VENV)/bin
ARTIFACT := dist/slo-eval.pyz

.DEFAULT_GOAL := help
.PHONY: help install fmt lint test check build clean

help: ## List available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-8s %s\n", $$1, $$2}'

install: $(VENV)/.installed ## Create .venv with pinned dev tools (pytest, ruff)

$(VENV)/.installed: requirements-dev.txt
	@$(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 11))' || \
		{ echo "error: Python 3.11+ required, found: $$($(PYTHON) --version 2>&1)" >&2; exit 1; }
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --quiet --disable-pip-version-check -r requirements-dev.txt
	@touch $@

fmt: install ## Auto-format code and apply safe lint fixes
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

lint: install ## Check lint and formatting without changing files
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

test: install ## Run the test suite
	$(BIN)/python -m pytest

check: lint test ## Lint + tests (what CI runs)

build: ## Build dist/slo-eval.pyz (stdlib zipapp) and smoke-test it
	rm -rf build/zipapp $(ARTIFACT)
	mkdir -p build/zipapp dist
	cp -R src/slo_eval build/zipapp/
	find build/zipapp -name __pycache__ -prune -exec rm -rf {} +
	$(PYTHON) -m zipapp build/zipapp --main 'slo_eval.__main__:run' \
		--python '/usr/bin/env python3' --compress --output $(ARTIFACT)
	./$(ARTIFACT) --version

clean: ## Remove build outputs and caches (keeps .venv)
	rm -rf build dist .pytest_cache .ruff_cache
	find src tests -name __pycache__ -prune -exec rm -rf {} +
