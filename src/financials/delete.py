"""Delete an existing transaction or expected entry (financials delete).

Resolves the id in the committed ledger (hash id, full or unambiguous
prefix) or the expected register (e####), shows the row, and requires an
explicit confirmation (bare Enter is NOT yes). Actual rows are applied
as one DeleteMutation carrying the victim's full pre-image (the engine
verifies it against the live row and refuses loudly on mismatch).
Expected rows are removed from the expected register (no balance chain).

There is no undo; the journal IS the audit trail of what was removed.
"""

from __future__ import annotations

from datetime import date

from rich.console import Console
from rich.prompt import Prompt

from financials.commands import cmd_delete
from financials.model import (
  Transaction,
  find_entry,
  load_expected,
  save_expected,
)

console = Console()


def _print_row(t: Transaction) -> None:
  console.print(
    f"[dim]Te verwijderen:[/dim] {t.id}  {t.date}  {t.description}  "
    f"[{t.category}]  {t.amount_eur:+,.2f}"
  )


def _confirmed(t: Transaction) -> bool:
  """Explicit confirmation; bare Enter is never yes."""
  answer = (
    Prompt.ask("Verwijderen? (typ 'ja' om te bevestigen, Enter = annuleren)", default="")
    .strip()
    .lower()
  )
  return answer in {"ja", "j", "yes", "y"}


def _delete_expected(t: Transaction) -> int:
  _print_row(t)
  if not _confirmed(t):
    console.print("[yellow]Geannuleerd — niets verwijderd.[/yellow]")
    return 2
  expected = load_expected()
  save_expected([x for x in expected if x.id != t.id])
  console.print(f"[green]Verwijderd: {t.id}[/green]")
  return 0


def _delete_actual(t: Transaction) -> int:
  _print_row(t)
  if not _confirmed(t):
    console.print("[yellow]Geannuleerd — niets verwijderd.[/yellow]")
    return 2
  _mutation, deleted = cmd_delete(t.id)
  if not deleted:
    console.print(f"[red]Onbekende id: {t.id}[/red]")
    return 2
  console.print(f"[green]Verwijderd: {t.id}[/green]")
  if t.category == "Overdracht":
    console.print(
      "[yellow]Let op: Overdracht-verwijdering raakt ook de spaar-keten "
      "— controleer 'financials list'.[/yellow]"
    )
  return 0


def _pick_recent() -> str | None:
  """Bare 'delete': show the last 10 committed rows with ids, ask which."""
  from financials.ledger_view import view_rows

  rows = [t for t in view_rows() if t.date <= date.today().isoformat()]
  recent = rows[-10:]
  if not recent:
    console.print("[dim]Geen actuele rijen gevonden.[/dim]")
    return None
  console.print("[bold]Laatste 10 actuele rijen:[/bold]")
  for t in recent:
    console.print(
      f"  [dim]{t.id}[/dim]  {t.date}  {t.description}  [{t.category}]  {t.amount_eur:+,.2f}"
    )
  text = Prompt.ask("Id om te verwijderen (leeg = annuleren)", default="")
  return text.strip() or None


def delete_transaction(entry_id: str | None = None) -> int:
  """Delete a committed (hash id or prefix) or expected (e####) entry,
  with confirmation."""
  if entry_id is None:
    entry_id = _pick_recent()
    if entry_id is None:
      return 2
  from financials.commands import command_journal
  from financials.journal import load_ledger

  ledger = load_ledger(command_journal())
  entry, _ = ledger.find(entry_id)
  if entry is None:
    expected = find_entry(entry_id, [], load_expected())
    if expected is None:
      # resolver: a group (g####) — deleting the GROUP (members survive
      # untouched; un-group, not delete).
      from financials.groups import find_group, load_groups

      if find_group(load_groups(), entry_id) is not None:
        from financials.groups import delete_group

        return delete_group(entry_id)
      # last resolver: a projected rule instance (r:-hash) — deleting it
      # SUPPRESSES the instance (its date joins the rule's exceptions);
      # the rule keeps running for the remaining periods. Resolved FIRST:
      # a decline inside suppression is a cancel, not an unknown id.
      from financials.recurrence_cli import _find_instance, suppress_occurrence

      if _find_instance(entry_id) is not None:
        return suppress_occurrence(entry_id)
      console.print(f"[red]Onbekende id: {entry_id}[/red]")
      return 2
    return _delete_expected(expected)
  projected = Transaction(
    id=entry.id or "",
    date=entry.date.isoformat(),
    description=entry.description,
    category_raw=entry.category,
    category=entry.category,
    subcategory_raw="",
    subcategory="",
    amount_eur=entry.postings.get("checking"),
    status="actual",
    linked_id="",
    balance_checking=entry.balances.get("checking"),
    balance_savings=entry.balances.get("savings"),
  )
  return _delete_actual(projected)
