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


def _commit_expected(t: Transaction):
  """The commit core of _confirm_expected: the ConfirmMutation path (the
  e-row retires and the actual lands in one mutation), then the e-row is
  dropped — only after the confirm applied. Returns the committed entry
  (with its new hash id) or None (nothing applied; amount-less rows are
  refused upstream)."""
  from financials.commands import cmd_add, command_journal

  if t.amount_eur is None:
    return None
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
    return None
  expected = load_expected()
  save_expected([x for x in expected if x.id != t.id])
  return entry


def _confirm_expected(t: Transaction) -> int:
  """Zero-prompt confirm of a landed expected row: the same ConfirmMutation
  path as edit's all-Enter confirm (cmd_add with retires), then the e-row
  is dropped — only after the confirm applied."""
  if t.date > date.today().isoformat():
    console.print(
      f"[yellow]{t.id} is nog toekomst ({t.date}) — niets bevestigd. "
      "Aanpassen kan met 'financials edit'.[/yellow]"
    )
    return 2
  if t.amount_eur is None:
    console.print("[red]Deze rij heeft geen bedrag om te bevestigen.[/red]")
    return 2
  entry = _commit_expected(t)
  if entry is None:
    console.print("[red]Niet bevestigd: de boeking kon niet worden toegepast.[/red]")
    return 2
  balances = entry.balances
  console.print(
    f"[green]Bevestigd: {t.id} → {entry.id}[/green]  [dim]expected entry verwijderd[/dim]"
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
  """Confirm a rule instance (r:-hash). LANDED instances commit
  zero-prompt with the rule's own values (a covering commit supersedes
  the instance automatically, nothing to record). A FUTURE instance
  opens the prefilled prompts (the same walk as edit's landed branch:
  datum first, shiftable down to today) so an early-landed bill can be
  confirmed interactively; recurrence_cli's own future-date gate
  refuses an actual dated ahead of today."""
  from financials import recurrence_cli

  hit = recurrence_cli._find_instance(entry_id)
  if hit is None:
    return 2  # not a rule instance: the caller reports the unknown id
  rule_hit, occurrence_date = hit
  if occurrence_date > recurrence_cli.TODAY():
    # future instance: interactive confirm — datum first, Enter keeps
    return recurrence_cli._confirm_instance(rule_hit, occurrence_date)
  return recurrence_cli.commit_instance(
    rule_hit,
    occurrence_date,
    occurrence_date,
    rule_hit.description,
    rule_hit.category,
    rule_hit.amount,
  )


def _confirm_group(entry_id: str) -> int:
  """Zero-prompt confirm of a group's LANDED members, in (date, id)
  order: e-members commit via the standard expected path (each e#### is
  re-pointed to its new committed id in the membership list), r-members
  via the rule-instance path. Future or dangling members are skipped
  with a note; the group itself survives. Nothing to confirm -> exit 2
  with a message, a partially-confirmable group -> exit 0 with skips
  reported."""
  from financials.groups import find_group, load_groups, resolve_member, save_groups

  groups = load_groups()
  group = find_group(groups, entry_id)
  if group is None:
    console.print(f"[red]Onbekende groep: {entry_id}[/red]")
    return 2
  today_iso = date.today().isoformat()
  landed: list[tuple[str, Transaction]] = []
  for member_id in group.members:
    t = resolve_member(member_id)
    if t is None:
      console.print(f"[yellow]{member_id} is niet meer resolveerbaar — overgeslagen.[/yellow]")
      continue
    if t.date > today_iso:
      console.print(f"[yellow]{member_id} is nog toekomst ({t.date}) — overgeslagen.[/yellow]")
      continue
    if t.status == "actual":
      console.print(f"[dim]{member_id} is al actual — niets te doen.[/dim]")
      continue
    if t.amount_eur is None:
      console.print(f"[yellow]{member_id} heeft geen bedrag — overgeslagen.[/yellow]")
      continue
    landed.append((member_id, t))
  if not landed:
    console.print(f"[dim]{group.id}: niets om te bevestigen.[/dim]")
    return 2
  # Re-resolve the group from a FRESH load on every save (resolve_member
  # reboots the journal; the membership file is the mutable state).
  for member_id, t in sorted(landed, key=lambda pair: (pair[1].date, pair[0])):
    if t.status == "expected":
      entry = _commit_expected(t)
      if entry is not None:
        groups = load_groups()
        target = find_group(groups, group.id)
        if target is not None and member_id in target.members:
          # The e-row retired into a committed hash id: re-point.
          target.members = [entry.id or member_id if m == member_id else m for m in target.members]
          save_groups(groups)
    else:
      from financials.commands import command_journal
      from financials.journal import load_ledger
      from financials.recurrence_cli import _find_instance, commit_instance
      from financials.recurrences import AMOUNT_TOLERANCE, DAY_WINDOW

      hit = _find_instance(member_id)
      if hit is None:
        continue
      rule_hit, occurrence_date = hit
      # A superseded instance (already covered by a real commit) must not
      # double-commit: same cover criteria as the projection's superseding.
      covered = any(
        entry.category == rule_hit.category
        and entry.postings.get("checking") is not None
        and abs((entry.postings.get("checking") or 0.0) - rule_hit.amount) <= AMOUNT_TOLERANCE
        and abs((entry.date - occurrence_date).days) <= DAY_WINDOW
        for entry in load_ledger(command_journal()).transactions
      )
      if covered:
        console.print(f"[dim]{member_id} is al gedekt door een geboekte rij — overgeslagen.[/dim]")
        continue
      commit_instance(
        rule_hit,
        occurrence_date,
        occurrence_date,
        rule_hit.description,
        rule_hit.category,
        rule_hit.amount,
      )
  console.print(f"[green]Groep {group.id} bevestigd (voor zover geland).[/green]")
  return 0


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
  A group id (g####) confirms every LANDED member in sequence, re-pointing
  each e#### to its new committed id so the group survives confirmation.
  Bare confirm shows the OPEN section to pick from."""
  if entry_id is None:
    entry_id = _pick_open()
    if entry_id is None:
      return 2
  if entry_id.startswith("g") and entry_id[1:].isdigit():
    return _confirm_group(entry_id)
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
