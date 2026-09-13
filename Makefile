YOKER_FROM=../yoker
-include ~/.yoker/Makefile

# --- financials -----------------------------------------------------------

.PHONY: import run sync test

sync: ## Install/sync dependencies into the uv-managed virtual environment
	uv sync

test: ## Run the tests
	uv run pytest

# Using...

FINANCIALS=uv run financials

run: ## `make run ARGS="report --months 6"` or `make CMD=report ARGS="--months 6"`)
	$(FINANCIALS) $(CMD) $(ARGS)

report: ## Visual report (usage: make report ARGS="--months 6")
report: CMD=report
report: run

DAYS ?= 3
PROJECT ?= 14

view: ## Show Ledger
view: CMD=list
view: ARGS=--days $(DAYS) --project $(PROJECT)
view: run

TX?=unknown

add: ## Run the financials CLI in interactive mode
add: CMD=add
add: run

delete: ## Delete a transaction
delete: CMD=delete
delete: ARGS=$(TX)

edit: ## Edit a transaction
edit: CMD=edit
edit: ARGS=$(TX)
edit: run

# tools

size:
	find src/ | grep "\.py$$" | xargs wc -l | sort -rn
