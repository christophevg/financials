# AGENTS.md — financials

Project-specific facts for agents working in this repo. Cross-project
rules live in `AGENTS.global.md`; session notes in `SESSION.md`.

## What this is

`financials` — a personal cashflow management CLI (Dutch-language UI
strings): import a manually maintained `cashflow.tsv`, clean it up through
reviewed fix files, visualize, and forecast. Src-layout Python package,
`uv`-managed, entry point `financials.cli:main` (see `pyproject.toml`).

## Data & configuration (never commit these)

- All stores live under the config-driven `data_dir` (owner's: `~/.financials/data`):
  `transactions.json` (actuals), `expected.json` (one-off future entries),
  `recurrences.json` (recurring rules), `journal.jsonl` (append-only audit
  trail), `cleanup_fixes*` (reviewed fix files, renamed `*.applied` when applied).
- Config: `~/.financials.toml` (user) + `./financials.toml` (project) via
  the clevis cascade; keys: `categories`, `data_dir`. Repo carries generic
  defaults only. `config_loader.load_config` disables the CLI layer.
- `SESSION.md` is gitignored transient session state.

## Commands

- `make sync` / `make test` (env `TEST=` selects a file, e.g.
  `tests/test_entry.py`) / `make run CMD=<subcommand> ARGS="<flags>"`.
- `view`, `add`, `delete`, `edit`, `report` are thin wrappers over `run`.
- `yoker.toml` allowlist for the make tool: `run` → `[CMD, ARGS]`,
  `test` → `[TEST]`. No other env vars are allowed.
- The Makefile is owner-managed (write-protected): propose changes, never edit.

## Invariants (do not break)

- `transactions.json` is the only actuals store; expected one-offs live in
  `expected.json`; recurring rules in `recurrences.json` are **never
  materialized** — they expand dynamically at view time and real entries
  supersede rule instances per period.
- Every mutation (fix/edit/delete) goes through the fixes engine and is
  journaled to `journal.jsonl` (append-only).
- `import` refuses to overwrite an existing store without `--force`.
- New global fix ops MUST be added to `GLOBAL_OPS` in `fixes.py` and MUST
  be idempotent (return "nothing to do" when already applied).
- Categories come from config (`approved_categories()`); the data-quality
  tests lock data invariants and skip when no data file exists.

## Known gaps / next

- `financials recurrence` CLI (add/list/pause/resume) + history-based rule
  detector are not implemented yet; `recurrences.py` already has
  load/save/expand/supersede, and `list` already projects rule expansions.
- Makefile lacks the standard `check`/`lint`/`format` targets.