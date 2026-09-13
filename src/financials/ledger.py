"""Ledger listing: recent actuals + projection window (financials list).

Table: id | date | description | category | change | balance checking |
balance savings. The actual section covers the last X days; the projection
section covers the next Y days and is composed of entered expected entries
plus dynamic recurrence expansions (superseded per period by real entries).
Projected balances continue from the last known checking balance. Ids make
rows addressable by `financials edit <id>` (rule rows are virtual: no id).
"""

from __future__ import annotations

from datetime import date, timedelta

from rich import box
from rich.console import Console
from rich.table import Table

from financials.model import (
  STATUS_ACTUAL,
  Transaction,
  load_expected,
  load_transactions,
)
from financials.recurrences import expand_recurrences, load_recurrences

console = Console()


def _fmt(value: float | None) -> str:
  if value is None:
    return "—"
  return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _ledger_rows(rows: list[Transaction], start_checking: float | None) -> list[tuple]:
  """(id, date, description, category, change, checking, savings) with a
  running checking balance; recorded balances are used when present."""
  rendered = []
  checking = start_checking
  for t in sorted(rows, key=lambda t: t.date):
    if t.balance_checking is not None:
      checking = t.balance_checking
    elif checking is not None and t.amount_eur is not None:
      checking = round(checking + t.amount_eur, 2)
    rendered.append(
      (t.id, t.date, t.description, t.category, t.amount_eur, checking, t.balance_savings)
    )
  return rendered


def print_ledger(days: int, project: int) -> None:
  today = date.today()
  transactions = load_transactions()

  actual_rows = [
    t
    for t in transactions
    if t.date and t.status == STATUS_ACTUAL and "summary row" not in t.note
    and today - timedelta(days=days) <= date.fromisoformat(t.date) <= today
  ]

  dated = [t for t in transactions if t.date and "summary row" not in t.note]
  # Anchor = the chronologically-last row on-or-before today. Same-date rows
  # resolve by file order (source_line) so the day's last row anchors —
  # max() on date alone returns the first same-date row.
  anchor = max(
    (t for t in dated if date.fromisoformat(t.date) <= today),
    key=lambda t: (t.date, t.source_line),
    default=None,
  )
  start_checking = anchor.balance_checking if anchor else None

  window_start = today
  window_end = today + timedelta(days=project)
  virtual = expand_recurrences(
    load_recurrences(), transactions, window_start, window_end
  )
  # One-off expected entries live in their own register (e#### ids).
  # No lower bound: landed-but-unconfirmed (date <= today) and overdue
  # entries stay visible until they are confirmed into the actual store.
  expected = [
    t
    for t in load_expected()
    if t.date and date.fromisoformat(t.date) <= window_end
  ]
  projection = sorted(expected + virtual, key=lambda t: (t.date, t.id))

  console.print(
    f"[bold]Kasboek[/bold] [dim]— actuals: laatste {days} dagen, "
    f"projectie: komende {project} dagen (expected + regels; vervallen "
    f"entries blijven zichtbaar tot bevestigd)[/dim]"
  )
  table = Table(box=box.SIMPLE)
  table.add_column("Id", style="dim")
  table.add_column("Datum", justify="left")
  table.add_column("Omschrijving")
  table.add_column("Categorie")
  table.add_column("Verandering", justify="right")
  table.add_column("Checking", justify="right")
  table.add_column("Spaar", justify="right")

  for r in _ledger_rows(actual_rows, None):
    table.add_row(
      r[0], r[1], r[2], r[3], f"{r[4]:+,.2f}", _fmt(r[5]), _fmt(r[6])
    )

  if projection:
    table.add_section()
    running = start_checking
    running_savings = anchor.balance_savings if anchor else None
    for t in projection:
      if running is not None and t.amount_eur is not None:
        running = round(running + t.amount_eur, 2)
      if running_savings is not None and t.amount_eur is not None and t.category == "Overdracht":
        running_savings = round(running_savings - t.amount_eur, 2)
      table.add_row(
        "" if t.id.startswith("r:") else t.id,
        t.date,
        t.description + (" (regel)" if t.id.startswith("r:") else ""),
        t.category,
        f"{t.amount_eur:+,.2f}",
        _fmt(running),
        _fmt(running_savings),
        style="dim",
      )
  console.print(table)
  if not actual_rows and not projection:
    console.print("[dim]Geen rijen in deze vensters.[/dim]")