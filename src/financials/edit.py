"""Edit an existing transaction or expected entry (financials edit).

Resolves the id in the committed ledger (hash id, full or unambiguous
prefix) or the expected register (e####), then prompts Datum/
Omschrijving/Categorie/Bedrag pre-filled with the current values —
Enter keeps the value. Actual rows are applied as one UpdateMutation
(full replacement of the mutable fields; the id and linked references
ride along; date moves re-insert with propagated balances) — the
engine's update = delete + insert. Expected rows are edited in the
expected register (no balance chain to replay); a landed expected row
(date <= today) confirms into the ledger as a ConfirmMutation.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from rich.console import Console
from rich.prompt import Prompt

from financials.ask import ABORT, ask_category
from financials.commands import cmd_add, cmd_delete, cmd_update, command_journal
from financials.expected import add_expected
from financials.journal import Journal  # noqa: F401 — typing
from financials.model import (
  Transaction,
  find_entry,
  load_expected,
  save_expected,
)

console = Console()

RETRY_LIMIT = 3


def _boot(journal: Journal | None = None):
  from financials.commands import command_journal
  from financials.journal import load_ledger

  return load_ledger(journal if journal is not None else command_journal())


def _ask_amount(current: float, allow_zero: bool = False) -> float | None:
  """Prompt a signed amount pre-filled with the current one. None = abort.
  allow_zero (expected rows): 0 is a legal placeholder — reserve the
  entry now, fill in the real amount when it lands."""
  for _ in range(RETRY_LIMIT):
    text = Prompt.ask(
      "Bedrag (positief = inkomsten, negatief = uitgaven)",
      default=f"{current:.2f}",
    )
    try:
      value = round(float(text.replace(",", ".").strip()), 2)
    except ValueError:
      console.print("[red]Ongeldig bedrag.[/red]")
      continue
    if value == 0 and round(current, 2) != 0 and not allow_zero:
      console.print("[red]Bedrag 0 is niet toegelaten.[/red]")
      continue
    return value
  return None


def _collect_changes(t: Transaction, actual: bool) -> dict | None:
  """Prompt all four fields pre-filled; return only the changed fields
  (keyed by Transaction field name). None = user aborted."""
  console.print("[dim]Enter behoudt de huidige waarde.[/dim]")
  iso_date = ""
  for _ in range(RETRY_LIMIT):
    text = Prompt.ask("Datum (YYYY-MM-DD)", default=t.date).strip()
    try:
      date.fromisoformat(text)
    except ValueError:
      console.print(f"[red]Ongeldige datum: {text!r}[/red]")
      continue
    iso_date = text
    break
  else:
    return None
  if actual and iso_date > date.today().isoformat():
    console.print(
      "[dim]Datum in de toekomst — deze rij wordt verplaatst naar het "
      "expected-register (verwijderd als actual, toegevoegd als expected).[/dim]"
    )

  description = Prompt.ask("Omschrijving", default=t.description).strip()
  if not description:
    console.print("[red]Omschrijving mag niet leeg zijn.[/red]")
    return None

  category = ask_category(current=t.category)
  if category == ABORT or category is None:
    return None

  if t.amount_eur is None:
    console.print("[red]Deze rij heeft geen bedrag om te bewerken.[/red]")
    return None
  amount = _ask_amount(t.amount_eur, allow_zero=not actual)
  if amount is None:
    return None

  changes: dict = {}
  if iso_date != t.date:
    changes["date"] = iso_date
  if description != t.description:
    changes["description"] = description
  if category != t.category:
    changes["category"] = category
  if amount != t.amount_eur:
    changes["amount_eur"] = amount
  return changes


def _print_current(t: Transaction) -> None:
  console.print(
    f"[dim]Huidige rij:[/dim] {t.id}  {t.date}  {t.description}  "
    f"[{t.category}]  {t.amount_eur:+,.2f}"
  )


def _pick_recent() -> str | None:
  """Bare 'edit': show the last 10 committed rows with ids and ask which."""
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
  text = Prompt.ask("Id om te bewerken (leeg = annuleren)", default="")
  return text.strip() or None


def _edit_expected(t: Transaction) -> int:
  _print_current(t)
  changes = _collect_changes(t, actual=False)
  if changes is None:
    console.print("[yellow]Geannuleerd — niets gewijzigd.[/yellow]")
    return 2
  if not changes and t.date <= date.today().isoformat():
    # Landed as expected: an all-Enter confirm is the normal case.
    return _confirm_to_actual(t, {})
  if not changes:
    console.print("[dim]Niets gewijzigd.[/dim]")
    return 0
  # Today-or-past = the entry has landed: confirm it into the ledger.
  new_date = changes.get("date", t.date)
  if new_date <= date.today().isoformat():
    return _confirm_to_actual(t, changes)

  expected = load_expected()
  updated = [replace(t, **changes) if x.id == t.id else x for x in expected]
  save_expected(updated)
  console.print(f"[green]Bijgewerkt: {t.id}[/green]")
  return 0


def _confirm_to_actual(t: Transaction, changes: dict) -> int:
  """Edit-to-today-or-past on an expected row: append a ConfirmMutation
  (the e-row retires and the actual lands in one mutation) and drop the
  expected row — only after the confirm applied."""
  iso_date = changes.get("date", t.date)
  description = changes.get("description", t.description)
  category = changes.get("category", t.category)
  amount = changes.get("amount_eur", t.amount_eur)
  if amount is None:
    console.print("[red]Niet omgezet: deze rij heeft geen bedrag.[/red]")
    return 2

  journal = command_journal()
  mutation, entry = cmd_add(
    iso_date=iso_date,
    description=description,
    category=category,
    amount=amount,
    retires=[t.id],
    journal=journal,
  )
  if entry is None:
    console.print("[red]Niet omgezet: geen voorgaande rij gevonden.[/red]")
    return 2

  expected = load_expected()
  save_expected([x for x in expected if x.id != t.id])
  balances = entry.balances
  console.print(
    f"[green]Bevestigd: {t.id} → {entry.id}[/green]  [dim]expected entry verwijderd[/dim]"
  )
  console.print(
    f"[dim]checking {balances.get('checking', 0.0):,.2f}, spaar "
    f"{balances.get('savings', 0.0):,.2f}[/dim]"
  )
  if category == "Overdracht":
    console.print(
      "[yellow]Let op: Overdracht-boeking raakt ook de spaar-keten "
      "— controleer 'financials list'.[/yellow]"
    )
  return 0


def _move_to_expected(t: Transaction, new_date: str, changes: dict) -> int:
  """Edit-to-future-date: delete the committed entry, then add the
  equivalent expected entry — composed from the edited fields. The
  delete happens FIRST; when the expected add rejects, the recovery
  hint names 'financials expect'."""
  journal = command_journal()
  _mutation, deleted = cmd_delete(t.id, journal=journal)
  if not deleted:
    console.print(f"[red]Onbekende id: {t.id}[/red]")
    return 2
  result = add_expected(
    iso_date=new_date,
    description=changes.get("description", t.description),
    category=changes.get("category", t.category),
    amount=float(changes.get("amount_eur", t.amount_eur) or 0.0),  # prompt-validated
  )
  if result != 0:
    console.print(
      "[red]De actual-rij is verwijderd maar de expected entry is NIET "
      "aangemaakt — herstel met 'financials expect'.[/red]"
    )
    return result
  console.print(f"[green]Verplaatst: {t.id} → expected op {new_date}[/green]")
  if t.category == "Overdracht" or changes.get("category") == "Overdracht":
    console.print(
      "[yellow]Let op: Overdracht-verplaatsing raakt ook de spaar-keten "
      "— controleer 'financials list'.[/yellow]"
    )
  return 0


def _edit_actual(t: Transaction) -> int:
  _print_current(t)
  changes = _collect_changes(t, actual=True)
  if changes is None:
    console.print("[yellow]Geannuleerd — niets gewijzigd.[/yellow]")
    return 2
  if not changes:
    console.print("[dim]Niets gewijzigd.[/dim]")
    return 0

  # Future date = the row no longer belongs in the committed ledger. Move
  # it: delete here, re-create as expected at the new date.
  if "date" in changes and changes["date"] > date.today().isoformat():
    return _move_to_expected(t, changes["date"], changes)

  journal = command_journal()
  mutation, entry = cmd_update(
    target=t.id,
    iso_date=changes.get("date", t.date),
    description=changes.get("description", t.description),
    category=changes.get("category", t.category),
    amount=float(changes.get("amount_eur", t.amount_eur) or 0.0),
    journal=journal,
  )
  if entry is None:
    console.print("[red]Niet bijgewerkt: doelrij verdwenen.[/red]")
    return 1
  balances = entry.balances
  console.print(
    f"[green]Bijgewerkt: {entry.id}[/green]  [dim]checking "
    f"{balances.get('checking', 0.0):,.2f}, spaar {balances.get('savings', 0.0):,.2f}[/dim]"
  )
  return 0


def edit_transaction(entry_id: str | None = None) -> int:
  """Edit a committed (hash id or prefix) or expected (e####) entry
  interactively."""
  if entry_id is None:
    entry_id = _pick_recent()
    if entry_id is None:
      return 2
  ledger = _boot()
  entry, _ = ledger.find(entry_id)
  if entry is None:
    expected = find_entry(entry_id, [], load_expected())
    if expected is None:
      # resolver: a group (g####) — the group editor (label/rollup date/
      # category/note; membership via 'group add/remove').
      from financials.groups import find_group, load_groups

      if find_group(load_groups(), entry_id) is not None:
        from financials.groups import edit_group

        return edit_group(entry_id)
      # last resolver: a projected rule instance (r:-hash) — the owner's
      # two-option edit (rule vs occurrence-as-expected-exception).
      # Resolved FIRST: a decline inside the occurrence edit is a cancel,
      # not an unknown id.
      from financials.recurrence_cli import _find_instance, edit_occurrence

      if _find_instance(entry_id) is not None:
        return edit_occurrence(entry_id)
      console.print(f"[red]Onbekende id: {entry_id}[/red]")
      return 2
    return _edit_expected(expected)
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
  return _edit_actual(projected)
