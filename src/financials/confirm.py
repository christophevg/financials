"""Confirm an open entry in one step (financials confirm).

The fast path for the view's OPEN section: a landed-but-unconfirmed
expected row (e####) or rule instance (r:-hash) is committed into the
journaled ledger with the zero-prompt idiom — the stored values are
taken as-is, no field prompts. The adjust path stays 'edit <id>'
(prefilled prompts); a wrong confirm is recoverable via delete/edit,
and the commit is journaled either way.

Resolver chain (same shape as edit/delete, exact ids only):
  t#### (committed hash)  -> already actual: idempotent no-op (exit 0)
  e#### (expected)        -> ConfirmMutation retires the e-row and the
                             actual lands in one mutation; the expected
                             row is dropped only after the confirm applied
  r:-hash (rule instance) -> AddMutation with the rule's own values; a
                             covering commit supersedes the instance
                             automatically, a drifted one is excepted
Bare 'confirm' shows the OPEN section (the confirmation backlog) and
asks which row to confirm. Anything unresolvable exits 2 with the
standard 'Onbekende id' message.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from rich import box
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from financials.model import Transaction, find_entry, load_expected, save_expected

console = Console()


def _confirm_expected(t: Transaction) -> int:
  """Zero-prompt confirm of a landed expected row: the same ConfirmMutation
  path as edit's all-Enter confirm (cmd_add with retires), then the e-row
  is dropped — only after the confirm applied."""
  from financials.commands import cmd_add, command_journal

  if t.date > date.today().isoformat():
    console.print(
      f"[yellow]{t.id} is nog toekomst ({t.date}) — niets bevestigd. "
      "Aanpassen kan met 'financials edit'.[/yellow]"
    )
    return 2
  if t.amount_eur is None:
    console.print("[red]Deze rij heeft geen bedrag om te bevestigen.[/red]")
    return 2

  journal = command_journal()
  _mutation, entry = cmd_add(
    iso_date=t.date,
    description=t.description,
    category=t.category,
    amount=t.amount_eur,
    retires=[t.id],
    journal=journal,
  )
  if entry is None:
    console.print("[red]Niet bevestigd: de boeking kon niet worden toegepast.[/red]")
    return 2

  expected = load_expected()
  save_expected([x for x in expected if x.id != t.id])
  balances = entry.balances
  console.print(
    f"[green]Bevestigd: {t.id} → {entry.id}[/green]  "
    "[dim]expected entry verwijderd[/dim]"
  )
  console.print(
    f"[dim]checking {balances.get('checking', 0.0):,.2f}, spaar "
    f"{balances.get('savings', 0.0):,.2f}[/dim]"
  )
  if t.category == "Overdracht":
    console.print(
      "[yellow]Let op: Overdracht-boeking raakt ook de spaar-keten "
      "— controleer 'financials list'.[/yellow]"
    )
  return 0


def _confirm_instance(entry_id: str) -> int:
  """Zero-prompt confirm of a landed rule instance: commit with the
  rule's own values (date = the occurrence date); a covering commit
  supersedes the instance automatically, nothing to record."""
  from financials.recurrence_cli import _find_instance, commit_instance

  hit = _find_instance(entry_id)
  if hit is None:
    return 2  # not a rule instance: the caller reports the unknown id
  rule_hit, occurrence_date = hit
  if occurrence_date > date.today():
    console.print(
      f"[yellow]{entry_id} is nog toekomst ({occurrence_date.isoformat()}) "
      "— niets bevestigd. Niet gewenste instantie: 'financials delete "
      f"{entry_id}' onderdrukt ze.[/yellow]"
    )
    return 2
  return commit_instance(
    rule_hit,
    occurrence_date,
    occurrence_date,
    rule_hit.description,
    rule_hit.category,
    rule_hit.amount,
  )


def _pick_open() -> str | None:
  """Bare 'confirm': show the OPEN section (landed-but-unconfirmed
  expected rows + rule instances — the confirmation backlog) and ask
  which id to confirm. Empty = nothing to confirm."""
  from financials.ledger_view import open_rows

  rows = open_rows()
  if not rows:
    console.print("[dim]Niets openstaand om te bevestigen.[/dim]")
    return None
  table = Table(title="Openstaand (geland maar niet bevestigd)", box=box.SIMPLE)
  columns: list[tuple[str, Literal["left", "right"]]] = [
    ("Id", "left"),
    ("Datum", "left"),
    ("Omschrijving", "left"),
    ("Categorie", "left"),
    ("Bedrag", "right"),
  ]
  for column, justify in columns:
    table.add_column(column, justify=justify)
  for t in rows:
    table.add_row(t.id, t.date, t.description, t.category, f"{t.amount_eur:+,.2f}")
  console.print(table)
  text = Prompt.ask("Id om te bevestigen (leeg = annuleren)", default="")
  return text.strip() or None


def confirm_transaction(entry_id: str | None = None) -> int:
  """Confirm an open entry in one go: expected (e####) or rule instance
  (r:-hash) as-is, zero prompts. Committed ids are already actual (no-op).
  Bare confirm shows the OPEN section to pick from."""
  if entry_id is None:
    entry_id = _pick_open()
    if entry_id is None:
      return 2
  if entry_id.startswith("e") and entry_id[1:].isdigit():
    expected = find_entry(entry_id, [], load_expected())
    if expected is None:
      console.print(f"[red]Onbekende id: {entry_id}[/red]")
      return 2
    return _confirm_expected(expected)
  if entry_id.startswith("r:"):
    return _confirm_instance(entry_id)
  from financials.commands import command_journal
  from financials.journal import load_ledger

  ledger = load_ledger(command_journal())
  entry, _ = ledger.find(entry_id)
  if entry is not None:
    console.print(f"[dim]{entry_id} is al actual ({entry.date}) — niets te doen.[/dim]")
    return 0
  console.print(f"[red]Onbekende id: {entry_id}[/red]")
  return 2