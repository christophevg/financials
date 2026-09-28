# financials

Personal cashflow management: journal, visualize, and forecast a
manually maintained ledger — a durable event journal (with per-row
content-hash ids) plus a ledger checkpoint, seeded from a
one-time bootstrap of the historical `cashflow.tsv`.

The tool keeps an auditable, mechanical pipeline: the committed rows live
in the event journal, every later change is a journaled mutation
(add/confirm/update/delete, each rehash-verified), and the
forecast is built only from explicit, human-approved inputs (future entries
and recurring rules) — never from statistical guesswork.

## Commands

| Command | Purpose |
|---------|---------|
| `financials fix --file F` / `--all` | Apply reviewed cleanup-fix files (JSON or TOML, `cleanup_fixes*`), oldest first; every application is recorded to an append-only journal. |
| `financials add` | Add a transaction — interactive, fully flagged, or shorthand (see below). Future-dated entries go to the expected register automatically. |
| `financials expect` | Manage one-off future entries (`--date --description --category --amount`, `--list`, `--remove=<id>`). |
| `financials edit [ID]` / `financials delete [ID]` | Edit or delete a transaction (`t####`) or expected entry (`e####`), with confirmation; changes are replay-checked and journaled. |
| `financials confirm [ID]` | Confirm an open entry in one go: a landed expected entry (`e####`) or rule instance (`r:...`) commits as-is, zero prompts (the fast path; `edit <id>` is the adjust path). Bare `confirm` shows the OPEN section to pick from. |
| `financials list --days X --project Y` | Ledger view: actuals of the last X days + projection of the next Y days (expected one-offs + recurring-rule expansions, superseded per period by real entries). |
| `financials report [--months N] [--year Y] [--top N]` | Visual report: monthly flows, category breakdown, balance curve. |

### Shorthand quick-add

`financials add "[date] [amount] [Category:]description"` — e.g.

```
financials add "gisteren -25 Kafe"
financials add "-3d +1200 Salaris"
```

Dates accept ISO, `vandaag/today`, `morgen/tomorrow`, `gisteren/yesterday`,
or relative `±Nd` days (the `d` suffix is required — a bare `-25` is always
an amount). Amounts are signed or currency-marked; missing fields are
prompted, nothing is silently assumed.

## Configuration

Personal configuration lives outside the repo:

- `~/.financials.toml` — user level (personal categories, `data_dir`)
- `./financials.toml` — project level, overrides user

The repo carries only generic defaults (categories *Inkomsten*/*Uitgaven*,
data in `~/financials-data`), so it runs anywhere without leaking personal
data. All data stores (`transactions.json`, `expected.json`,
`recurrences.json`, `journal.jsonl`, `cleanup_fixes*`) live under the
configured `data_dir`.

## Development

The project uses [`uv`](https://docs.astral.sh/uv/) and a Makefile:

```
make sync        # install/sync dependencies into the uv-managed venv
make test        # run the test suite (incl. the data-quality gate)
make run CMD=report ARGS="--months 6"
make view        # ledger: last 3 days + 14-day projection (DAYS/PROJECT vars)
make size        # line counts per module
```

The data-quality tests run against the real `transactions.json` and skip
automatically when no data has been imported yet.

Run the CLI locally with `uv run financials --help`.