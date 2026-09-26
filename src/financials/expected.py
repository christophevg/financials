"""One-off expected (future) entries (financials expect).

Expected entries live in their own store (data/expected.json), the
non-recurring analogue of recurrences.json — separate from the actual
transactions, composed at view time.
"""

from __future__ import annotations

from datetime import date

from rich.console import Console

from financials.config import approved_categories
from financials.model import (
  STATUS_EXPECTED,
  Transaction,
  load_expected,
  save_expected,
)

console = Console()


def _next_id(expected: list[Transaction]) -> str:
  highest = 0
  for t in expected:
    if t.id.startswith("e") and t.id[1:].isdigit():
      highest = max(highest, int(t.id[1:]))
  return f"e{highest + 1:04d}"


def add_expected_row_from_fix(fix: dict) -> str:
  """(Re-)create a one-off expected entry from a fix file's own fields
  (fix-file op add_expected_row). Returns the new entry's id."""
  entry = Transaction(
    id=_next_id(load_expected()),
    date=fix["date"],
    description=fix["description"],
    category_raw=fix["category"],
    category=fix["category"],
    subcategory_raw="",
    subcategory="",
    amount_eur=fix["amount_eur"],
    status=STATUS_EXPECTED,
    linked_id="",
    source_line=fix.get("source_line", 0),
    note=fix.get("note", ""),
  )
  expected = load_expected()
  expected.append(entry)
  save_expected(expected)
  return entry.id


def add_expected(
  iso_date: str,
  description: str,
  category: str,
  amount: float,
  dry_run: bool = False,
) -> int:
  """Add a one-off expected entry. Returns 0 on success, 2 on rejection.
  dry_run=True validates and previews without saving."""
  try:
    date.fromisoformat(iso_date)
  except ValueError:
    console.print(f"[red]Niet opgeslagen: ongeldige datum {iso_date!r}[/red]")
    return 2
  if not description.strip():
    console.print("[red]Niet opgeslagen: omschrijving mag niet leeg zijn.[/red]")
    return 2
  if category not in approved_categories():
    console.print(f"[red]Niet opgeslagen: categorie {category!r} is niet goedgekeurd.[/red]")
    return 2
  if amount is None:
    console.print("[red]Niet opgeslagen: bedrag ontbreekt.[/red]")
    return 2
  if iso_date <= date.today().isoformat():
    console.print(
      "[red]Niet opgeslagen: een expected entry moet in de toekomst liggen "
      "(gebruik 'financials add' voor feitelijke transacties).[/red]"
    )
    return 2

  expected = load_expected()
  entry = Transaction(
    id=_next_id(expected),
    date=iso_date,
    description=description.strip(),
    category_raw=category,
    category=category,
    subcategory_raw="",
    subcategory="",
    amount_eur=amount,
    status=STATUS_EXPECTED,
    linked_id="",
    balance_checking=None,
    balance_savings=None,
    flags=[],
    source_line=0,
    note="expected entry",
  )
  expected.append(entry)
  if dry_run:
    console.print("[yellow]Dry-run — niets opgeslagen. Zou toevoegen:[/yellow]")
    console.print(
      f"  {entry.date}  {entry.description}  [{entry.category}]  {entry.amount_eur:+,.2f}"
    )
    return 0
  save_expected(expected)
  console.print(f"[green]Opgeslagen als {entry.id}[/green]")
  console.print(
    f"  {entry.date}  {entry.description}  [{entry.category}]  {entry.amount_eur:+,.2f}"
  )
  return 0


def list_expected() -> int:
  expected = load_expected()
  if not expected:
    console.print("[dim]Geen expected entries.[/dim]")
    return 0
  console.print(f"[bold]Expected entries ({len(expected)})[/bold]")
  for entry in sorted(expected, key=lambda t: t.date):
    console.print(
      f"  {entry.id}  {entry.date}  {entry.description}  "
      f"[{entry.category}]  {entry.amount_eur:+,.2f}"
    )
  return 0


def remove_expected(entry_id: str) -> int:
  expected = load_expected()
  remaining = [t for t in expected if t.id != entry_id]
  if len(remaining) == len(expected):
    console.print(f"[red]Onbekende id: {entry_id}[/red]")
    return 2
  save_expected(remaining)
  console.print(f"[green]Verwijderd: {entry_id}[/green]")
  return 0
