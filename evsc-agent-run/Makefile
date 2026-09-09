# evse-ingest development workflow.
#
# Thin wrapper: every target delegates to the real script or compose file, so
# there is one obvious entry point without a second source of truth. Run `make`
# with no arguments for the target list.
#
# Host requirements: docker (with compose v2) and python3. Neither suite needs
# bash 4, jq, or PHP on the host -- linting runs inside the container.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

CHAR_DIR := tests/characterization
PROP_DIR := tests/property
SRC_FILES := src/ingest.php src/db.php bin/worker.php

# Subset filter for the characterization suite: make test-char CASES='09-*'
CASES ?=
# Property suite knobs: make test-prop SCENARIOS=100 SEED=20260907
SCENARIOS ?= 30
SEED ?=
FILE ?=

.PHONY: help verify test test-char test-prop char-list char-record prop-replay \
        lint docs-check up down nuke logs ps shell db rebuild smoke clean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nevse-ingest — make targets\n\nUsage:\n  make \033[36m<target>\033[0m\n"} \
		/^[a-zA-Z_0-9-]+:.*?##/ { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 } \
		/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)
	@printf "\nCommon flows:\n"
	@printf "  refactor    make test          # must be green before AND after\n"
	@printf "  bugfix      make test          # read the diffs, then: make char-record CONFIRM=1\n"
	@printf "  pre-merge   make verify\n\n"

##@ Quality gates

verify: lint docs-check test ## Full pre-merge gate: lint, docs, both suites
	@printf "\n==> verify: all gates passed\n"

test: test-char test-prop ## Run both test suites (the regression gate)

test-char: ## Characterization suite: verify baselines byte-for-byte (~6 min)
	@printf "==> characterization suite\n"
	@cd $(CHAR_DIR) && ./run.py $(CASES)

test-prop: ## Property suite: generated scenarios vs invariants (~10 min)
	@printf "==> property suite\n"
	@cd $(PROP_DIR) && ./property_test.py --scenarios $(SCENARIOS) \
		$(if $(SEED),--seed $(SEED),)

char-list: ## List characterization cases with descriptions
	@cd $(CHAR_DIR) && ./run.py --list

char-record: ## Re-record baselines — BEHAVIOR CHANGE ONLY, needs CONFIRM=1
	@if [ "$(CONFIRM)" != "1" ]; then \
		printf 'refusing to re-record.\n\n'; \
		printf 'Recording baselines asserts that the behavior change is intended.\n'; \
		printf 'During a refactor this is how an unreviewed change gets committed.\n\n'; \
		printf 'If behavior really did change on purpose:\n'; \
		printf '  1. make test            # then read every tests/characterization/.results/*.diff\n'; \
		printf '  2. make char-record CONFIRM=1 [CASES=%s25-*%s]\n' "'" "'"; \
		printf '  3. commit code and baseline diff together\n\n'; \
		printf 'See tests/README.md for the full bugfix workflow.\n'; \
		exit 1; \
	fi
	@cd $(CHAR_DIR) && ./run.py --record $(CASES)

prop-replay: ## Replay a saved counterexample: make prop-replay FILE=corpus/x.json
	@if [ -z "$(FILE)" ]; then \
		printf 'usage: make prop-replay FILE=corpus/<file>.json\n\n'; \
		printf 'available counterexamples:\n'; \
		ls -1 $(PROP_DIR)/corpus/*.json 2>/dev/null | sed 's|^$(PROP_DIR)/|  |' \
			|| printf '  (none saved)\n'; \
		exit 1; \
	fi
	@cd $(PROP_DIR) && ./property_test.py --replay "$(FILE)"

lint: ## Syntax-check every source file (runs PHP inside the container)
	@printf "==> lint\n"
	@for f in $(SRC_FILES); do \
		printf '  %s: ' "$$f"; \
		docker compose run --rm --no-deps app php -l "$$f" 2>&1 | tail -1; \
	done

docs-check: ## Validate every internal doc link and heading anchor
	@printf "==> docs\n"
	@python3 scripts/check-docs.py

##@ Run and operate

up: ## Start the stack (builds the image)
	docker compose up -d --build

down: ## Stop the stack, keeping the data volume
	docker compose down

nuke: ## Stop the stack AND drop the data volume (required for schema changes)
	docker compose down -v

rebuild: ## Rebuild and restart the worker after a code change
	docker compose up -d --build app

logs: ## Follow worker output
	docker compose logs -f app

ps: ## Show stack status
	docker compose ps

shell: ## Open a shell in the worker container
	docker compose exec app bash

db: ## Open a MySQL shell against the service database
	docker compose exec db mysql -u ingest_svc -pingest_svc evse

smoke: ## Inject one frame end-to-end and report what happened
	@python3 scripts/smoke.py

##@ Housekeeping

clean: ## Remove test artefacts and tear down every stack this repo starts
	@printf "==> tearing down stacks\n"
	-@docker compose down -v --remove-orphans >/dev/null 2>&1
	-@docker compose -p evse-charz -f $(CHAR_DIR)/docker-compose.test.yml \
		down -v --remove-orphans >/dev/null 2>&1
	-@docker compose -p evse-prop -f $(CHAR_DIR)/docker-compose.test.yml \
		down -v --remove-orphans >/dev/null 2>&1
	@printf "==> removing transient files\n"
	@rm -rf $(CHAR_DIR)/.results $(CHAR_DIR)/.env.runtime $(CHAR_DIR)/__pycache__
	@rm -rf $(PROP_DIR)/.env.runtime $(PROP_DIR)/lib/__pycache__ tests/harness/__pycache__
	@printf "    baselines and corpus counterexamples kept (they are committed artefacts)\n"
