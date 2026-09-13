"""Import: mechanical normalization of cashflow.tsv into data/transactions.json.

The importer normalizes formats only — dates to ISO, Dutch euro amounts to
decimals, the raw column layout to the transaction schema — and validates the
data by replaying both balance columns. It deliberately does NOT interpret
content: raw category values pass through untouched, anomalies are flagged
instead of fixed, and flagged rows are resolved interactively in the cleanup
phase.

Re-runnable and idempotent: refuses to overwrite an existing
data/transactions.json unless --force is given, so a re-run can never
silently destroy manual cleanup edits.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.table import Table

from financials.model import (
  FLAG_BALANCE_CHECKING,
  FLAG_BALANCE_SAVINGS,
  FLAG_BAD_AMOUNT,
  FLAG_BAD_DATE,
  FLAG_MISSING_CATEGORY,
  FLAG_MISSING_DATE,
  STATUS_ACTUAL,
  STATUS_EXPECTED,
  Transaction,
  save_transactions,
  transactions_file,
)
from financials.config import data_dir
from financials.parsing import (
  check_weekday,
  looks_like_date,
  parse_amount,
  parse_date,
  read_rows,
  split_category,
)

REPORT_FILE_NAME = "import_report.md"

DEFAULT_TSV = Path("cashflow.tsv")


def report_file() -> Path:
  return data_dir() / REPORT_FILE_NAME


AMOUNT_TOLERANCE = 0.005
ROUNDING_TOLERANCE = 0.10
BALANCE_ANOMALY_MARKER = "balance anomaly documented in source"
SUMMARY_ROWS = {"Min", "Max"}

console = Console()


def _padded(cells: list[str]) -> list[str]:
  """Pad/truncate a raw row to exactly 6 cells."""
  padded = cells + [""] * (6 - len(cells))
  return padded[:6]


def _build_transaction(index: int, lineno: int, cells: list[str], today: str) -> Transaction:
  padded = _padded(cells)
  first = padded[0].strip()
  flags: list[str] = []
  note_parts: list[str] = []

  iso = parse_date(first) if first else None
  if iso is not None:
    date_iso = iso
    _, description, category_raw, amount_text, checking_text, savings_text = padded
    weekday_note = check_weekday(first)
    if weekday_note:
      note_parts.append(weekday_note)
  else:
    # Continuation row: the date cell is absent. Three source shapes occur:
    # (a) an empty first cell followed by the 5 content cells,
    # (b) the 5 content cells directly (first cell is the description),
    # (c) all 6 cells present but the first is junk (a marker like '?').
    date_iso = ""
    flags.append(FLAG_MISSING_DATE)
    if first and looks_like_date(first):
      flags.append(FLAG_BAD_DATE)
    if len(cells) == 6 and padded[0] == "":
      # (a) empty date cell, content starts at cell 1
      description, category_raw, amount_text, checking_text, savings_text = padded[1:6]
    elif len(cells) == 6:
      # (c) junk in the date slot, all 6 columns present
      description, category_raw, amount_text, checking_text, savings_text = padded[1:6]
      note_parts.append("date cell contained junk; columns realigned")
    else:
      # (b) 5 content cells, description first
      description, category_raw, amount_text, checking_text, savings_text = padded[:5]

  category, subcategory = split_category(category_raw)
  if not category:
    flags.append(FLAG_MISSING_CATEGORY)

  amount = parse_amount(amount_text)
  if amount is None:
    flags.append(FLAG_BAD_AMOUNT)

  status = STATUS_EXPECTED if date_iso and date_iso > today else STATUS_ACTUAL
  if description in SUMMARY_ROWS:
    note_parts.append("summary row (aggregate, not a transaction)")

  return Transaction(
    id=f"t{index:04d}",
    date=date_iso,
    description=description.strip(),
    category_raw=category_raw,
    category=category,
    subcategory_raw=subcategory,
    subcategory=subcategory,
    amount_eur=amount,
    status=status,
    linked_id="",
    balance_checking=parse_amount(checking_text),
    balance_savings=parse_amount(savings_text),
    flags=flags,
    source_line=lineno,
    note="; ".join(note_parts),
  )


def _replay_balances(transactions: list[Transaction]) -> tuple[int, int, int]:
  """Verify recorded running balances for both accounts.

  Checking: each recorded balance must equal the previous row's recorded
  balance plus the row amount. Savings: each recorded balance must either be
  unchanged, differ by exactly -amount (money moved between the accounts), or
  differ by ±|amount| (interest booked directly into savings). Differences
  below ROUNDING_TOLERANCE are tolerated as rounding artifacts. The recorded
  balance always becomes the new anchor, so a single typo never cascades.
  Rows marked 'expected' are projections, not ground truth, and are skipped;
  same for rows whose note contains the balance-anomaly marker.

  Returns (number of checks performed, number of mismatches, number of
  tolerated rounding artifacts).
  """
  if not transactions:
    return 0, 0, 0
  # Balance flags are derived: strip stale ones so repeated replays are
  # idempotent (fixes may run on data that already carries balance flags).
  for t in transactions:
    if FLAG_BALANCE_CHECKING in t.flags:
      t.flags.remove(FLAG_BALANCE_CHECKING)
    if FLAG_BALANCE_SAVINGS in t.flags:
      t.flags.remove(FLAG_BALANCE_SAVINGS)
  prev_checking = transactions[0].balance_checking
  prev_savings = transactions[0].balance_savings
  checks = mismatches = tolerated = 0
  for t in transactions[1:]:
    if t.status == STATUS_EXPECTED or BALANCE_ANOMALY_MARKER in t.note:
      prev_checking = t.balance_checking if t.balance_checking is not None else prev_checking
      prev_savings = t.balance_savings if t.balance_savings is not None else prev_savings
      continue
    if t.balance_checking is not None:
      if prev_checking is not None and t.amount_eur is not None:
        checks += 1
        expected = round(prev_checking + t.amount_eur, 2)
        if abs(expected - t.balance_checking) > AMOUNT_TOLERANCE:
          t.flags.append(FLAG_BALANCE_CHECKING)
          mismatches += 1
      prev_checking = t.balance_checking
    if t.balance_savings is not None:
      if prev_savings is not None:
        checks += 1
        unchanged = abs(t.balance_savings - prev_savings) <= AMOUNT_TOLERANCE
        moved = (
          t.amount_eur is not None
          and abs(t.balance_savings - round(prev_savings - t.amount_eur, 2)) <= AMOUNT_TOLERANCE
        )
        interest = (
          t.amount_eur is not None
          and abs(t.balance_savings - round(prev_savings + abs(t.amount_eur), 2)) <= AMOUNT_TOLERANCE
        )
        rounding = abs(t.balance_savings - prev_savings) <= ROUNDING_TOLERANCE
        if not (unchanged or moved or interest):
          if rounding:
            tolerated += 1
          else:
            t.flags.append(FLAG_BALANCE_SAVINGS)
            mismatches += 1
      prev_savings = t.balance_savings
  return checks, mismatches, tolerated


def _write_report(
  transactions: list[Transaction],
  checks: int,
  mismatches: int,
  tsv_path: Path,
  today: str,
) -> None:
  dated = [t.date for t in transactions if t.date]
  categories = Counter(t.category_raw if t.category_raw else "(leeg)" for t in transactions)
  flagged = [t for t in transactions if t.flags]

  lines = [
    "# Import report",
    "",
    f"Generated: {today} — source: `{tsv_path}`",
    "",
    "## Totals",
    "",
    "| metric | value |",
    "|---|---|",
    f"| rows | {len(transactions)} |",
    f"| dated rows | {len(dated)} |",
    f"| undated rows (flagged) | {len(transactions) - len(dated)} |",
    f"| status actual | {sum(1 for t in transactions if t.status == STATUS_ACTUAL)} |",
    f"| status expected (future-dated) | {sum(1 for t in transactions if t.status == STATUS_EXPECTED)} |",
    f"| flagged rows | {len(flagged)} |",
    f"| distinct raw categories | {len(categories)} |",
    f"| date range | {min(dated)} → {max(dated)} |",
    "",
    "## Balance validation",
    "",
    f"- checks performed: {checks}",
    f"- mismatches: {mismatches}",
    "",
  ]
  if mismatches == 0:
    lines.append("All recorded balances were replayed successfully — the import is arithmetically consistent.")
  else:
    lines += [
      "| id | source line | description | account | recorded | expected |",
      "|---|---|---|---|---|---|",
    ]
    for t in flagged:
      if FLAG_BALANCE_CHECKING in t.flags or FLAG_BALANCE_SAVINGS in t.flags:
        account = "checking" if FLAG_BALANCE_CHECKING in t.flags else "savings"
        lines.append(
          f"| {t.id} | {t.source_line} | {t.description} | {account} | {t.balance_checking if account == 'checking' else t.balance_savings} | see JSON |"
        )
  lines += [
    "",
    "## Category census (raw values, untouched — input for the cleanup phase)",
    "",
    "| raw category | count |",
    "|---|---|",
  ]
  for raw, count in categories.most_common():
    lines.append(f"| `{raw}` | {count} |")
  lines += [
    "",
    "## Flagged rows (to resolve interactively in the cleanup phase)",
    "",
  ]
  if flagged:
    for t in flagged:
      note_suffix = f" — note: {t.note}" if t.note else ""
      lines.append(
        f"- **{t.id}** (source line {t.source_line}) “{t.description}” — flags: {', '.join(t.flags)}{note_suffix}"
      )
  else:
    lines.append("None.")

  report = report_file()
  report.parent.mkdir(parents=True, exist_ok=True)
  report.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _print_summary(
  transactions: list[Transaction], checks: int, mismatches: int, tolerated: int
) -> None:
  dated = [t.date for t in transactions if t.date]
  table = Table(title="Import summary", show_header=False, pad_edge=False)
  table.add_column("metric", style="bold")
  table.add_column("value")
  table.add_row("rows", str(len(transactions)))
  table.add_row("dated / undated", f"{len(dated)} / {len(transactions) - len(dated)}")
  table.add_row(
    "status actual / expected",
    f"{sum(1 for t in transactions if t.status == STATUS_ACTUAL)} /"
    f" {sum(1 for t in transactions if t.status == STATUS_EXPECTED)}",
  )
  table.add_row("flagged rows", str(sum(1 for t in transactions if t.flags)))
  table.add_row("distinct raw categories", str(len({t.category_raw for t in transactions})))
  table.add_row(
    "balance checks", f"{checks} performed, {mismatches} mismatches, {tolerated} rounding artifacts"
  )
  console.print(table)
  if mismatches:
    console.print(f"[red]Balance replay found {mismatches} mismatches — see {report_file()}[/red]")
  else:
    console.print("[green]Balance replay verified all recorded balances.[/green]")
  console.print(f"Wrote {transactions_file()} and {report_file()}.")
  console.print("Next: resolve flagged rows and unify categories (cleanup phase).")


def run_import(tsv_path: Path = DEFAULT_TSV, force: bool = False) -> None:
  if transactions_file().exists() and not force:
    console.print(
      "[red]data/transactions.json already exists.[/red] Re-importing would overwrite it, "
      "including any manual cleanup edits. Use [bold]--force[/bold] to overwrite anyway."
    )
    raise SystemExit(2)

  today = date.today().isoformat()
  rows = read_rows(tsv_path)
  transactions = [
    _build_transaction(i, lineno, cells, today) for i, (lineno, cells) in enumerate(rows, start=1)
  ]
  checks, mismatches, tolerated = _replay_balances(transactions)
  save_transactions(transactions)
  _write_report(transactions, checks, mismatches, tsv_path, today)
  _print_summary(transactions, checks, mismatches, tolerated)
