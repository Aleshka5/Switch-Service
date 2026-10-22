# Human entry point. Make starts commands; it does not supervise the process.
# Production restarts belong to systemd (deploy/switch.service) or a container unit.

PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
HOST ?= 127.0.0.1
PORT ?= 8095
CONFIG ?= config/services.yaml
RUNTIME ?= fake
DB ?= var/switch.sqlite

.DEFAULT_GOAL := help
.PHONY: help venv install test lint run check clean

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-10s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

venv: $(BIN)/python ## Create .venv and install the package

install: venv ## Alias of venv

test: $(BIN)/python ## Run unit tests
	$(BIN)/pytest -q

lint: $(BIN)/python ## Run ruff
	$(BIN)/ruff check switch_service tests

run: $(BIN)/python ## Serve the API on localhost (fake runtime unless RUNTIME=podman)
	mkdir -p $(dir $(DB))
	SWITCH_RUNTIME=$(RUNTIME) SWITCH_DB=$(DB) $(BIN)/switch-service serve \
		--host $(HOST) --port $(PORT) --config $(CONFIG)

check: $(BIN)/python ## Validate CONFIG and print the triangle
	$(BIN)/switch-service check --config $(CONFIG)

clean: ## Remove the local venv, caches and the dev database
	rm -rf $(VENV) .pytest_cache .ruff_cache var
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
