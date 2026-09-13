"""Delete an existing transaction or expected entry (financials delete).

Resolves the id in either store (t#### = actual, e#### = expected),
shows the row, and requires an explicit confirmation (bare Enter is
NOT yes). Actual rows are applied through the in-memory change-set
engine (remove_row + rebase_checking, from-guarded, replay-checked)
and journaled to data/journal.jsonl. Expected rows are removed
directly (no balance chain to replay).

There is no undo; the journal.jsonl audit trail records what was removed.
"""

from __future__ import annotations

from rich.console import Console
from rich.prompt import Prompt

from financials.fixes import apply_ops
from financials.importer import _replay_balances
from financials.model import (
  STATUS_ACTUAL,
  Transaction,
  load_expected,
  load_transactions,
)

console = Console()


def build_delete_fixes(t: Transaction) -> list[dict]:
  """Journal entries for deleting one row (either register)."""
  return [
    {
      "op": "remove_row",
      "id": t.id,
      "reason": f"day-to-day delete via 'financials delete {t.id}'",
    },
    # remove_row only removes; without this, downstream recorded checking
    # balances of a mid-history delete go stale (replay only CHECKS them).
    {
      "id": "all",
      "op": "rebase_checking",
      "reason": f"chain rebase after delete of {t.id}",
    },
  ]


def _print_row(t: Transaction) -> None:
  console.print(
    f"[dim]Te verwijderen:[/dim] {t.id}  {t.date}  {t.description}  "
    f"[{t.category}]  {t.amount_eur:+,.2f}"
  )


def _confirmed(t: Transaction) -> bool:
  """Explicit confirmation; bare Enter is never yes."""
  answer = Prompt.ask(
    f"Verwijderen? (typ 'ja' om te bevestigen, Enter = annuleren)", default=""
  ).strip().lower()
  return answer in {"ja", "j", "yes", "y"}


def _delete_expected(t: Transaction) -> int:
  _print_row(t)
  if not _confirmed(t):
    console.print("[yellow]Geannuleerd — niets verwijderd.[/yellow]")
    return 2
  expected = load_expected()
  save = [x for x in expected if x.id != t.id]
  from financials.model import save_expected

  save_expected(save)
  console.print(f"[green]Verwijderd: {t.id}[/green]")
  return 0


def _delete_actual(t: Transaction) -> int:
  _print_row(t)
  if not _confirmed(t):
    console.print("[yellow]Geannuleerd — niets verwijderd.[/yellow]")
    return 2
  apply_ops(build_delete_fixes(t), source=f"delete {t.id}")

  checks, mismatches, tolerated = _replay_balances(load_transactions())
  if mismatches:
    console.print(
      f"[red]Balans-replay: {mismatches} mismatches na verwijdering van "
      f"{t.id} — controleer 'financials list'.[/red]"
    )
    return 1
  console.print(
    f"[green]Verwijderd: {t.id}[/green]  [dim]"
    f"(balans-replay: {checks} checks, {tolerated} afrondings-artefacten)[/dim]"
  )
  if t.category == "Overdracht":
    console.print(
      "[yellow]Let op: Overdracht-verwijdering raakt ook de spaar-keten "
      "— controleer 'financials list'.[/yellow]"
    )
  return 0


def _pick_recent() -> str | None:
  """Bare 'delete': show the last 10 actual rows with ids, ask which."""
  transactions = [t for t in load_transactions() if t.status == STATUS_ACTUAL]
  recent = transactions[-10:]
  if not recent:
    console.print("[dim]Geen actuele rijen gevonden.[/dim]")
    return None
  console.print("[bold]Laatste 10 actuele rijen:[/bold]")
  for t in recent:
    console.print(
      f"  [dim]{t.id}[/dim]  {t.date}  {t.description}  "
      f"[{t.category}]  {t.amount_eur:+,.2f}"
    )
  text = Prompt.ask("Id om te verwijderen (leeg = annuleren)", default="")
  return text.strip() or None


def delete_transaction(entry_id: str | None = None) -> int:
  """Delete an actual (t####) or expected (e####) entry, with confirmation."""
  if entry_id is None:
    entry_id = _pick_recent()
    if entry_id is None:
      return 2
  for t in load_transactions():
    if t.id == entry_id:
      return _delete_actual(t)
  for t in load_expected():
    if t.id == entry_id:
      return _delete_expected(t)
  console.print(f"[red]Onbekende id: {entry_id}[/red]")
  return 2