"""Entry of new transactions (financials add).

Two paths:
- interactive: `financials add` walks through date/description/category/
  amount and validates the typed balance against the projected one;
- non-interactive: `financials add --date=... --description=... --category=...
  --amount=... [--checking=...] [--dry-run]` for scripted use.

Every new record continues the balance chain from the last known balances
and is inserted in date order (after existing rows of the same date).
"""

from __future__ import annotations

from datetime import date

from rich.console import Console
from rich.prompt import Prompt

from financials.ask import ABORT, ask_category
from financials.expected import add_expected
from financials.model import (
  APPROVED_CATEGORIES,
  STATUS_ACTUAL,
  STATUS_EXPECTED,
  Transaction,
  load_transactions,
  save_transactions,
)
from financials.shorthand import Shorthand, ShorthandError, parse_shorthand

console = Console()

RETRY_LIMIT = 3


def _next_id(transactions: list[Transaction]) -> str:
  """Next sequential id based on the highest existing numeric id."""
  highest = 0
  for t in transactions:
    if t.id.startswith("t") and t.id[1:].isdigit():
      highest = max(highest, int(t.id[1:]))
  return f"t{highest + 1:04d}"


def _predecessor_index(transactions: list[Transaction], iso_date: str) -> int:
  """Index of the last row in file order whose date is on-or-before iso_date
  (undated rows are never predecessors). -1 when none qualifies."""
  last_le = -1
  for index, t in enumerate(transactions):
    if t.date and t.date <= iso_date:
      last_le = index
  return last_le


def _insert_position(transactions: list[Transaction], iso_date: str) -> int:
  """Insertion index keeping the balance chain contiguous: directly after
  the chain predecessor (the last row dated on-or-before iso_date)."""
  return _predecessor_index(transactions, iso_date) + 1


def _validate_amount(text: str) -> float | None:
  try:
    amount = round(float(text.replace(",", ".")), 2)
  except ValueError:
    return None
  return amount if amount != 0 else None


def _build(
  transactions: list[Transaction],
  iso_date: str,
  description: str,
  category: str,
  amount: float,
  checking: float | None,
) -> tuple[Transaction | None, str]:
  """Validate and construct the transaction. Returns (transaction, error)."""
  try:
    date.fromisoformat(iso_date)
  except ValueError:
    return None, f"ongeldige datum: {iso_date!r}"
  if not description.strip():
    return None, "omschrijving mag niet leeg zijn"
  if category not in APPROVED_CATEGORIES:
    return None, f"categorie {category!r} is niet in de goedgekeurde lijst"

  dated = [t for t in transactions if t.date]
  if not dated:
    return None, "geen gedateerde rijen gevonden; kan balansen niet doorzetten"
  # Chain-last row = max over (date, file order). Bare max(date) returns the
  # FIRST row of the latest date on a tie — its savings may differ from the
  # chain-last row's (e.g. a same-day transfer row). The ledger's anchor had
  # this same fix; _build must key identically.
  last = max(dated, key=lambda t: (t.date, t.source_line))
  if last.balance_checking is None or last.balance_savings is None:
    return None, f"laatste rij ({last.id}) heeft geen balansen om door te zetten"

  # Historic entries are allowed: they chain from the last row dated
  # on-or-before the new date; rows after the insertion point are rebased
  # by the difference between the new row's end balance and the
  # predecessor's balance.
  predecessor_index = _predecessor_index(transactions, iso_date)
  predecessor = transactions[predecessor_index] if predecessor_index >= 0 else None
  if predecessor is None or predecessor.balance_checking is None:
    return None, "geen voorgaande rij met checking-saldo gevonden voor deze datum"

  projected = round(predecessor.balance_checking + amount, 2)
  if checking is None:
    checking = projected
  elif abs(checking - projected) >= 0.005:
    return None, (
      f"checking-saldo {checking:,.2f} komt niet overeen met het "
      f"voortgezette saldo {projected:,.2f} (van {predecessor.balance_checking:,.2f} "
      f"op {predecessor.date}, {amount:+,.2f})"
    )

  status = STATUS_ACTUAL
  if iso_date > date.today().isoformat():
    return None, (
      "datum ligt in de toekomst — future entries horen in het expected-register: "
      "gebruik 'financials expect'"
    )
  transaction = Transaction(
    id=_next_id(transactions),
    date=iso_date,
    description=description.strip(),
    category_raw=category,
    category=category,
    subcategory_raw="",
    subcategory="",
    amount_eur=amount,
    status=status,
    linked_id="",
    balance_checking=checking,
    balance_savings=last.balance_savings,
    flags=[],
    source_line=0,
    note="toegevoegd via financials add",
  )
  return transaction, ""


def _prompt(
  transactions: list[Transaction],
  seed: Shorthand | None = None,
) -> tuple[str, str, str, float, float | None] | None:
  """Interactive prompt loop. Returns entry values, or None on abort.
  `seed` pre-fills prompts from a parsed quick-add shorthand."""
  dated = [t for t in transactions if t.date]
  last = max(dated, key=lambda t: t.date) if dated else None

  console.print(
    "[bold]Nieuwe transactie[/bold] [dim](leeg antwoord = standaardwaarde, 'q' = annuleren)[/dim]"
  )
  if last:
    console.print(
      f"[dim]Laatste rij: {last.date} {last.description} — checking "
      f"{last.balance_checking:,.2f}, spaar {last.balance_savings:,.2f}[/dim]"
    )
    if date.today().isoformat() < last.date:
      console.print(
        "[yellow]Let op: de laatste rij ligt in de toekomst; kies een datum "
        "erna om de balansen te kunnen doorzetten.[/yellow]"
      )

  default_date = seed.date if seed and seed.date else max(
    date.today().isoformat(), last.date if last else ""
  )
  iso_date = Prompt.ask("Datum (YYYY-MM-DD)", default=default_date)
  if iso_date == "q":
    return None
  try:
    is_future = date.fromisoformat(iso_date) > date.today()
  except ValueError:
    is_future = False  # invalid format: let _build produce the error
  if is_future:
    console.print(
      "[dim]Toekomstige datum — opgeslagen in het expected-register "
      "(geen saldo-doorvoer).[/dim]"
    )
  description = Prompt.ask(
    "Omschrijving", default=(seed.description if seed and seed.description else "")
  )
  if description == "q":
    return None
  category = ask_category(current=(seed.category if seed and seed.category else ""))
  if category == ABORT or category is None:
    return None
  if seed and seed.amount is not None:
    amount = seed.amount
  else:
    amount_text = Prompt.ask(
      "Bedrag (positief = inkomsten, negatief = uitgaven, bv. -38.93)"
    )
    if amount_text == "q":
      return None
    amount = _validate_amount(amount_text)
    if amount is None:
      console.print("[red]Ongeldig bedrag (0 is niet toegelaten).[/red]")
      return None

  if last and last.balance_checking is not None and not is_future:
    projected = round(last.balance_checking + amount, 2)
    console.print(f"[dim]Geprojecteerd checking-saldo: {projected:,.2f}[/dim]")
    for attempt in range(1, RETRY_LIMIT + 1):
      checking_text = Prompt.ask(
        "Nieuw checking-saldo (leeg = geprojecteerd)", default=f"{projected:.2f}"
      )
      if checking_text == "q":
        return None
      try:
        checking = round(float(checking_text.replace(",", ".")), 2)
      except ValueError:
        console.print("[red]Ongeldig saldo.[/red]")
        continue
      if abs(checking - projected) < 0.005:
        return iso_date, description, category, amount, checking
      console.print(
        f"[red]Komt niet overeen met {projected:,.2f} (poging {attempt}/{RETRY_LIMIT}).[/red]"
      )
    console.print("[red]Te vaak mislukt — geannuleerd.[/red]")
    return None
  return iso_date, description, category, amount, None


def add_transaction(
  iso_date: str | None = None,
  description: str | None = None,
  category: str | None = None,
  amount: float | None = None,
  checking: float | None = None,
  dry_run: bool = False,
  shorthand: str | None = None,
) -> int:
  """Add a transaction. Three paths:
  - fully flagged: builds directly;
  - shorthand: "gisteren -25 Kafe" pre-seeds the interactive prompts
    (anything the shorthand omits is asked, Enter accepts the seed);
  - bare: fully interactive.
  Returns 0 on success, 2 on rejection/abort."""
  transactions = load_transactions()
  seed: Shorthand | None = None
  if shorthand:
    try:
      seed = parse_shorthand(shorthand)
    except ShorthandError as error:
      console.print(f"[red]Niet opgeslagen: {error}[/red]")
      return 2
    console.print(
      f"[dim]Shorthand: {seed.date or '(datum gevraagd)'}  "
      f"{seed.category or '(categorie gevraagd)'}  "
      f"{seed.description or '(omschrijving gevraagd)'}  "
      f"{seed.amount if seed.amount is not None else '(bedrag gevraagd)'}[/dim]"
    )
    iso_date = iso_date if iso_date is not None else seed.date
    description = (
      description if description is not None else seed.description
    )
    category = category if category is not None else seed.category
    amount = amount if amount is not None else seed.amount
  if (
    iso_date is not None
    and description is not None
    and category is not None
    and amount is not None
    and iso_date > date.today().isoformat()
  ):
    # Date-based dispatch: future → expected register (fully-specified
    # flag/shorthand path; partial values fall through to the prompts,
    # whose dispatch handles the future case once all fields are known).
    console.print("[dim]→ expected-register (financials expect).[/dim]")
    return add_expected(
      iso_date=iso_date,
      description=description,
      category=category,
      amount=amount,
      dry_run=dry_run,
    )
  if iso_date is None or description is None or category is None or amount is None:
    prompted = _prompt(
      transactions,
      seed=seed if (iso_date is None or description is None or category is None or amount is None) else None,
    )
    if prompted is None:
      console.print("[yellow]Geannuleerd — niets opgeslagen.[/yellow]")
      return 2
    p_date, p_description, p_category, p_amount, p_checking = prompted
    # Date-based dispatch: future → expected register, today/past → actual.
    if p_date > date.today().isoformat():
      console.print("[dim]→ expected-register (financials expect).[/dim]")
      return add_expected(
        iso_date=p_date,
        description=p_description,
        category=p_category,
        amount=p_amount,
        dry_run=dry_run,
      )
    transaction, error = _build(
      transactions, p_date, p_description, p_category, p_amount, p_checking
    )
  else:
    transaction, error = _build(transactions, iso_date, description, category, amount, checking)
  if error:
    console.print(f"[red]Niet opgeslagen: {error}[/red]")
    return 2

  position = _insert_position(transactions, transaction.date)
  if dry_run:
    console.print("[yellow]Dry-run — niets opgeslagen. Zou toevoegen:[/yellow]")
  else:
    delta = round(transaction.amount_eur, 2)
    for later in transactions[position:]:
      if later.balance_checking is not None:
        later.balance_checking = round(later.balance_checking + delta, 2)
    transactions.insert(position, transaction)
    save_transactions(transactions)
    console.print(f"[green]Opgeslagen als {transaction.id}[/green]")
  console.print(
    f"  {transaction.date}  {transaction.description}  [{transaction.category}]  "
    f"{transaction.amount_eur:+,.2f} → checking {transaction.balance_checking:,.2f}, "
    f"spaar {transaction.balance_savings:,.2f}  (status: {transaction.status})"
  )
  prev = transactions[position - 1] if position > 0 else None
  after = "na de laatste rij" if prev is None else f"na {prev.id} ({prev.date})"
  console.print(f"[dim]Positie {position} ({after}).[/dim]")
  return 0