# AGENTS.md — financials

Project-specific facts for agents working in this repo. Cross-project
rules live in `AGENTS.global.md`; session notes in `SESSION.md`.

## What this is

`financials` — a personal cashflow management CLI (Dutch-language UI
strings): a durable event journal (seeded by a one-time bootstrap of the
historical `cashflow.tsv`) with a ledger checkpoint, reviewed fix files
for batch cleanup, visualization, and forecast. Src-layout Python package,
`uv`-managed, entry point `financials.cli:main` (see `pyproject.toml`).

## Personal Information Discipline

This repo is public (under the owner's real GitHub login): the owner's
financial reality must never leak into it. The rules apply to ALL
tracked files — source, tests, docs, scripts, and commit messages.

- **Fictional data only.** Nothing extracted from the live stores
  (`~/.financials/data`) may appear in tracked files: no real
  descriptions or merchant/counterparty names, no real amounts or
  balances, no real dates tied to real descriptions, and no live config
  values (category names can be personal too). Tests and docs use
  synthetic fixtures only (e.g. Salaris, Huur, Boodschappen, round
  numbers).
- **High-risk spots (where real traces actually entered):** test
  fixtures copied from live rows; incident/design narratives in docs;
  pinned tip balances in scripts, gates or docstrings; live-data tests
  that assert concrete values.
- **Live-data gates assert structure, never values:** presence of
  balances, fold ≡ checkpoint agreement, row-count floors — a
  hardcoded real value goes stale with every use of the app AND is a
  privacy leak.
- **Before ANY commit:** review the staged diff (`git diff --cached`)
  and confirm explicitly to the owner that it carries no personal
  information. Stage explicit paths (never `git add --all`), and keep
  the confirmation one line: state what was checked, not that "it's
  probably fine".
- **A violation is its own commit:** if personal information is found
  (in the diff or in history), STOP and report to the owner —
  sanitization and any history rewrite are owner-approved actions,
  never silent fixes.

## Data & configuration (never commit these)

- All stores live under the config-driven `data_dir` (owner's: `~/.financials/data`):
  `journal.jsonl` (append-only audit trail; the committed ledger),
  `ledger.json` (checkpoint), `expected.json` (one-off future entries),
  `recurrences.json` (recurring rules), `groups.json` (row rollups),
  `cleanup_fixes*` (reviewed fix files, renamed `*.applied` when applied).
- Config: `~/.financials.toml` (user) + `./financials.toml` (project) via
  the clevis cascade; keys: `categories`, `data_dir`. Repo carries generic
  defaults only. `config_loader.load_config` disables the CLI layer.
- `SESSION.md` is gitignored transient session state.
- `groups.json` — named rollups of rows (credit-card statements): Group
  dataclass in `groups.py`, ids `g####`; members resolve across the three
  id families via `groups.resolve_member`; NOT journaled (organization
  metadata like rules/expected). The view hides members and shows one
  `⧉ <label> (n)` rollup at the rollup date (group's own date, else
  members' max) — uncommitted members re-time the total there; fully
  committed groups roll up display-only in the actuals window.
  `confirm <g-id>` multi-confirms landed members and re-points e-ids.

## Commands

- `make sync` / `make test` (env `TEST=` selects a file, e.g.
  `tests/test_entry.py`) / `make check` (ruff + mypy + pytest) /
  `make format` (ruff format + autofix) /
  `make run CMD=<subcommand> ARGS="<flags>"`.
- `view`, `add`, `delete`, `edit`, `report` are thin wrappers over `run`.
- CLI: `confirm [ID]` = zero-prompt confirm of open entries (landed
  `e####` / `r:-hash`; committed `t####` = no-op; `g####` = group
  multi-confirm; bare = OPEN-section picker). Fast path; `edit <id>`
  stays the adjust path. Core: `src/financials/confirm.py`; rule commits
  go through `recurrence_cli.commit_instance` (shared with edit's
  interactive confirm); OPEN rows come from `ledger_view.open_rows`.
- `yoker.toml` allowlist for the make tool: `run` → `[CMD, ARGS]`,
  `test` → `[TEST]`, `lint` → `[LINT_FLAGS]`. Only these env vars are
  allowed.
- The Makefile is owner-managed (write-protected): propose changes, never edit.

## Invariants (do not break)

- `transactions.json` is the only actuals store; expected one-offs live in
  `expected.json`; recurring rules in `recurrences.json` are **never
  materialized** — they expand dynamically at view time and real entries
  supersede rule instances per period.
- Every mutation (add/confirm/update/delete) goes through the fixes
  engine and is journaled to `journal.jsonl` (append-only).
- New global fix ops MUST be added to `GLOBAL_OPS` in `fixes.py` and MUST
  be idempotent (return "nothing to do" when already applied).
- Categories come from config (`approved_categories()`); the data-quality
  tests lock data invariants and skip when no data file exists.
- Every store a flow READS must be isolated in tests (conftest covers
  journal + expected; groups/recurrences patches are per-test file) —
  stores resolve at CALL time through the `*_file()` choke points in
  `model.py`/`fixes.py`, never via module-level imports.

## Known gaps / next

- `financials recurrence` CLI landed (list/add/pause/resume/remove/edit/
  detect) long ago; detector quality tuning against the owner's real
  history is still open.
- `financials group` CLI landed (list/show/create/add/remove/edit/delete
  + `confirm <g-id>` + edit/delete g-id resolvers). Open: partially-
  committed groups show committed members individually (no mixed split
  rendering yet); `groups.resolve_member`'s last-resort re-derivation of
  edited-rule instance ids re-expands every active rule per unknown id
  (fine at this scale, worth a cache if rules grow).
- Footer hint idea (owner, phase 7): "regels adres je via (prefix van
  de) omschrijving".
- Possible follow-ups the owner noted in passing: red for negative
  Verandering cells + footer "Onder nul" lines (list only today).
- migration/ dir still holds repair backups (events*.jsonl, *.bak) —
  owner said "delete in a few days" (as of 09-21).
