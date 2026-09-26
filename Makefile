YOKER_FROM=../yoker
-include ~/.yoker/Makefile

# --- financials -----------------------------------------------------------

.PHONY: import run sync test check lint format typecheck

sync: ## Install/sync dependencies into the uv-managed virtual environment
	uv sync

test: ## Run tests (usage: make test / optional: TEST=file|file:test_name)
	@if [ -d tests ] && [ -n "$$(find tests -name 'test_*.py' 2>/dev/null)" ]; then \
	  uv run pytest $(if $(TEST),$(TEST),); \
	else \
	  echo "No tests yet (agent/skill-driven repo) — skipping."; \
	fi

check: lint typecheck test ## Run all quality checks

lint: ## Check code for linting issues
	uv run ruff check $(LINT_FLAGS) src tests

format: ## Format code and fix linting issues
	uv run ruff format src tests
	uv run ruff check --fix src tests

typecheck: ## Run type checking
	uv run mypy src

# Using...

FINANCIALS=uv run financials

run: ## `make run ARGS="report --months 6"` or `make CMD=report ARGS="--months 6"`)
	$(FINANCIALS) $(CMD) $(ARGS)

report: ## Visual report (usage: make report ARGS="--months 6")
report: CMD=report
report: run

DAYS ?= 3
PROJECT ?= 30
FILTER ?=

view: ## Show Ledger
view: CMD=list
view: ARGS=--days $(DAYS) --project $(PROJECT) $(if $(FILTER),--filter $(FILTER),)
view: run

TX?=unknown

add: ## Add a transaction in interactive mode
add: CMD=add
add: run

delete: ## Delete a transaction
delete: CMD=delete
delete: ARGS=$(TX)
delete: run

edit: ## Edit a transaction
edit: CMD=edit
edit: ARGS=$(TX)
edit: run

add-recurring: ## Add a recurring transaction
add-recurring: CMD=recurrence add
add-recurring: run

detect: ## Detect recurring transactions
detect: CMD=recurrence detect
detect: run

adopt: ## Add a recurring transaction
adopt: CMD=recurrence detect --take $(TX)
adopt: run

.PHONY: run report view add delete edit

# tools

size:
	find src/ | grep "\.py$$" | xargs wc -l | sort -rn
	find tests/ | grep "\.py$$" | xargs wc -l | sort -rn
	find scripts/ | grep "\.py$$" | xargs wc -l | sort -rn

# migration support

migration-boostrap:
	uv run python scripts/bootstrap_journal.py
