"""Edit an existing transaction or expected entry (financials edit).

Resolves the id in either store (t#### = actual in transactions.json,
e#### = expected in expected.json), then prompts Datum/Omschrijving/
Categorie/Bedrag pre-filled with the current values — Enter keeps the
value. Expected rows are edited in place (no balance chain to replay);
a landed expected row (date <= today) confirms into the actual register.
Actual rows are applied through the in-memory change-set engine
(from-guarded field ops, rebase_checking for amount/date changes,
replay-checked) and journaled to data/journal.jsonl.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from rich.console import Console
from rich.prompt import Prompt

from financials.ask import ABORT, ask_category
from financials.delete import build_delete_fixes
from financials.entry import _build, _insert_position
from financials.expected import add_expected
from financials.fixes import apply_ops
from financials.importer import _replay_balances
from financials.model import (
  APPROVED_CATEGORIES,
  STATUS_ACTUAL,
  Transaction,
  load_expected,
  load_transactions,
  save_expected,
  save_transactions,
)

console = Console()

RETRY_LIMIT = 3


def build_edit_fixes(t: Transaction, changes: dict) -> list[dict]:
  """Journal entries for editing one actual row. Empty changes -> no ops.
  Raises ValueError when an Overdracht amount would change (the savings
  chain has no rebase op — such edits need a reviewed manual fix file)."""
  if not changes:
    return []
  if "amount_eur" in changes and (
    t.category == "Overdracht" or changes.get("category") == "Overdracht"
  ):
    raise ValueError(
      f"{t.id}: Overdracht-bedrag wijzigen verschuift ook de spaar-keten — "
      "maak een handmatig fix-bestand aan (zie data/cleanup_fixes_*)."
    )
  fixes = [
    {
      "id": t.id,
      "field": field,
      "from": getattr(t, field),
      "to": value,
      "reason": f"day-to-day edit via 'financials edit {t.id}'",
    }
    for field, value in changes.items()
  ]
  if "date" in changes:
    fixes.append(
      {"id": "all", "op": "sort_by_date", "reason": f"date edited on {t.id}"}
    )
  if "amount_eur" in changes or "date" in changes:
    fixes.append(
      {
        "id": "all",
        "op": "rebase_checking",
        "reason": f"chain rebase after edit of {t.id}",
      }
    )
  return fixes


def find_entry(
  entry_id: str, transactions: list[Transaction], expected: list[Transaction]
) -> Transaction | None:
  """Resolve a t#### (actual) or e#### (expected) id across both stores."""
  for t in transactions:
    if t.id == entry_id:
      return t
  for t in expected:
    if t.id == entry_id:
      return t
  return None


def _ask_amount(current: float) -> float | None:
  """Prompt a signed amount pre-filled with the current one. None = abort."""
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
    if value == 0 and round(current, 2) != 0:
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
  amount = _ask_amount(t.amount_eur)
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
  """Bare 'edit': show the last 10 actual rows with ids and ask which one."""
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
  # Today-or-past = the entry has landed: confirm it into the actual
  # register (add first with chaining; remove only on success).
  new_date = changes.get("date", t.date)
  if new_date <= date.today().isoformat():
    return _confirm_to_actual(t, changes)

  expected = load_expected()
  updated = [
    replace(t, **changes) if x.id == t.id else x for x in expected
  ]
  save_expected(updated)
  console.print(f"[green]Bijgewerkt: {t.id}[/green]")
  return 0


def _confirm_to_actual(t: Transaction, changes: dict) -> int:
  """Edit-to-today-or-past on an expected row: create the actual
  transaction with balance chaining (entry machinery), then drop the
  expected row — only after the add succeeded."""
  iso_date = changes.get("date", t.date)
  description = changes.get("description", t.description)
  category = changes.get("category", t.category)
  amount = changes.get("amount_eur", t.amount_eur)
  if amount is None:
    console.print("[red]Niet omgezet: deze rij heeft geen bedrag.[/red]")
    return 2

  transactions = load_transactions()
  transaction, error = _build(
    transactions, iso_date, description, category, amount, None
  )
  if error or transaction is None:
    console.print(f"[red]Niet omgezet: {error}[/red]")
    return 2

  position = _insert_position(transactions, transaction.date)
  transaction.linked_id = t.id  # provenance: this actual came from e####
  delta = round(transaction.amount_eur, 2)
  for later in transactions[position:]:
    if later.balance_checking is not None:
      later.balance_checking = round(later.balance_checking + delta, 2)
  transactions.insert(position, transaction)
  save_transactions(transactions)

  expected = load_expected()
  save_expected([x for x in expected if x.id != t.id])
  checks, mismatches, _tolerated = _replay_balances(load_transactions())
  if mismatches:
    console.print(
      f"[red]Balans-replay: {mismatches} mismatches na omzetting van "
      f"{t.id} — controleer 'financials list'.[/red]"
    )
    return 1
  console.print(
    f"[green]Bevestigd: {t.id} → {transaction.id}[/green]  "
    f"[dim]expected entry verwijderd[/dim]"
  )
  if transaction.category == "Overdracht":
    console.print(
      "[yellow]Let op: Overdracht-boeking raakt ook de spaar-keten "
      "— controleer 'financials list'.[/yellow]"
    )
  return 0


def _move_to_expected(t: Transaction, new_date: str, changes: dict) -> int:
  """Edit-to-future-date: remove the actual (rebase chain), then add the
  equivalent expected entry — composed from the edited fields."""
  apply_ops(build_delete_fixes(t), source=f"move-to-expected {t.id}")

  checks, mismatches, tolerated = _replay_balances(load_transactions())
  if mismatches:
    console.print(
      f"[red]Balans-replay: {mismatches} mismatches — de rij is verwijderd "
      f"maar de expected entry is NIET aangemaakt; herstel met 'financials "
      f"expect --date={new_date} --description={t.description!r} "
      f"--category={t.category} --amount={t.amount_eur}'.[/red]"
    )
    return 1
  result = add_expected(
    iso_date=new_date,
    description=changes.get("description", t.description),
    category=changes.get("category", t.category),
    amount=changes.get("amount_eur", t.amount_eur),
  )
  if result != 0:
    console.print(
      "[red]De actual-rij is verwijderd maar de expected entry is NIET "
      "aangemaakt — herstel met 'financials expect'.[/red]"
    )
    return result
  console.print(
    f"[green]Verplaatst: {t.id} → expected op {new_date}[/green]  [dim]"
    f"(balans-replay: {checks} checks, {tolerated} afrondings-artefacten)[/dim]"
  )
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

  # Future date = the row no longer belongs in the actual store. Move it:
  # remove here (rebase the chain), re-create as expected at the new date.
  if "date" in changes and changes["date"] > date.today().isoformat():
    return _move_to_expected(t, changes["date"], changes)

  try:
    fixes = build_edit_fixes(t, changes)
  except ValueError as error:
    console.print(f"[red]Niet bewerkt: {error}[/red]")
    return 2

  apply_ops(fixes, source=f"edit {t.id}")

  checks, mismatches, tolerated = _replay_balances(load_transactions())
  if mismatches:
    console.print(
      f"[red]Balans-replay: {mismatches} mismatches — de bewerking is "
      "toegepast maar de keten klopt niet; controleer 'financials list'.[/red]"
    )
    return 1
  console.print(
    f"[green]Bijgewerkt: {t.id}[/green]  [dim]"
    f"(balans-replay: {checks} checks, {tolerated} afrondings-artefacten)[/dim]"
  )
  return 0


def edit_transaction(entry_id: str | None = None) -> int:
  """Edit an actual (t####) or expected (e####) entry interactively."""
  if entry_id is None:
    entry_id = _pick_recent()
    if entry_id is None:
      return 2
  transactions = load_transactions()
  for t in transactions:
    if t.id == entry_id:
      return _edit_actual(t)
  for t in load_expected():
    if t.id == entry_id:
      return _edit_expected(t)
  console.print(f"[red]Onbekende id: {entry_id}[/red]")
  return 2
