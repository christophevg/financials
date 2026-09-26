"""Entry of new transactions (financials add).

Two paths:
- interactive: `financials add` walks through date/description/category/
  amount and validates the typed balance against the projected one;
- non-interactive: `financials add --date=... --description=... --category=...
  --amount=... [--checking=...] [--dry-run]` for scripted use.

Every new record is an AddMutation appended to the canonical journal and
applied by the ledger's engine: balance chaining and date ordering are
the engine's (date-positioned insert with propagation). The reported id
is the mutation's content-hash id.
"""

from __future__ import annotations

from datetime import date

from rich.console import Console
from rich.prompt import Prompt

from financials.ask import ABORT, ask_category
from financials.commands import cmd_add, projected_checking
from financials.config import approved_categories
from financials.expected import add_expected
from financials.journal import Journal, Ledger, Transaction  # noqa: F401 — typing
from financials.shorthand import Shorthand, ShorthandError, parse_shorthand

console = Console()

RETRY_LIMIT = 3


def command_journal():
  """The canonical journal (delegates to commands; a module attribute so
  tests can patch `financials.entry.command_journal` directly)."""
  from financials.commands import command_journal as _resolver

  return _resolver()


def _chain_last_entry(ledger: Ledger) -> Transaction | None:
  """The ledger's chain-last entry (its list is date-ordered, so the
  last element with a date is the tip)."""
  dated = [t for t in ledger.transactions if t.date]
  return dated[-1] if dated else None


def _boot(journal: Journal | None = None) -> Ledger:
  from financials.journal import load_ledger

  return load_ledger(journal if journal is not None else command_journal())


def _validate_amount(text: str) -> float | None:
  try:
    amount = round(float(text.replace(",", ".")), 2)
  except ValueError:
    return None
  return amount if amount != 0 else None


def _validate(
  iso_date: str,
  description: str,
  category: str,
  amount: float,
  ledger: Ledger,
) -> str:
  """Validation shared by the flagged and prompted paths. Returns an
  error string, empty when the entry may proceed."""
  try:
    date.fromisoformat(iso_date)
  except ValueError:
    return f"ongeldige datum: {iso_date!r}"
  if not description.strip():
    return "omschrijving mag niet leeg zijn"
  if category not in approved_categories():
    return f"categorie {category!r} is niet in de goedgekeurde lijst"
  if amount == 0:
    return "bedrag 0 is niet toegelaten"
  if iso_date > date.today().isoformat():
    return (
      "datum ligt in de toekomst — future entries horen in het expected-register: "
      "gebruik 'financials expect'"
    )
  if ledger.transactions:
    parent, _idx = ledger.find_parent(iso_date)
    if parent is None:
      return "geen voorgaande rij gevonden voor deze datum; kan balansen niet doorzetten"
  return ""


def _prompt(
  ledger: Ledger,
  seed: Shorthand | None = None,
) -> tuple[str, str, str, float, float | None] | None:
  """Interactive prompt loop. Returns entry values, or None on abort.
  `seed` pre-fills prompts from a parsed quick-add shorthand."""
  from financials.entry import _chain_last_entry

  last = _chain_last_entry(ledger)

  console.print(
    "[bold]Nieuwe transactie[/bold] [dim](leeg antwoord = standaardwaarde, 'q' = annuleren)[/dim]"
  )
  if last is not None:
    checking = last.balances.get("checking")
    savings = last.balances.get("savings")
    console.print(
      f"[dim]Laatste rij: {last.date.isoformat()} {last.description} — checking "
      f"{checking:,.2f}, spaar {savings:,.2f}[/dim]"
    )
    if date.today().isoformat() < last.date.isoformat():
      console.print(
        "[yellow]Let op: de laatste rij ligt in de toekomst; kies een datum "
        "erna om de balansen te kunnen doorzetten.[/yellow]"
      )

  last_date = last.date.isoformat() if last is not None else ""
  default_date = seed.date if seed and seed.date else max(date.today().isoformat(), last_date)
  iso_date = Prompt.ask("Datum (YYYY-MM-DD)", default=default_date)
  if iso_date == "q":
    return None
  try:
    is_future = date.fromisoformat(iso_date) > date.today()
  except ValueError:
    is_future = False  # invalid format: let _validate produce the error
  if is_future:
    console.print(
      "[dim]Toekomstige datum — opgeslagen in het expected-register (geen saldo-doorvoer).[/dim]"
    )
  description = Prompt.ask(
    "Omschrijving", default=(seed.description if seed and seed.description else "")
  )
  if description == "q":
    return None
  category = ask_category(current=(seed.category if seed and seed.category else ""))
  if category == ABORT or category is None:
    return None
  amount: float
  if seed and seed.amount is not None:
    amount = seed.amount
  else:
    amount_text = Prompt.ask("Bedrag (positief = inkomsten, negatief = uitgaven, bv. -38.93)")
    if amount_text == "q":
      return None
    parsed = _validate_amount(amount_text)
    if parsed is None:
      console.print("[red]Ongeldig bedrag (0 is niet toegelaten).[/red]")
      return None
    amount = parsed

  # Project from the ledger's date-positioned predecessor of the typed
  # date — the same row the engine will chain from.
  if not is_future:
    projected, predecessor = projected_checking(ledger, iso_date, amount)
    if projected is not None and predecessor is not None:
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
  journal: Journal | None = None,
) -> int:
  """Add a transaction. Three paths:
  - fully flagged: validates and appends directly;
  - shorthand: "gisteren -25 Kafe" pre-seeds the interactive prompts
    (anything the shorthand omits is asked, Enter accepts the seed);
  - bare: fully interactive.
  Returns 0 on success, 2 on rejection/abort."""
  ledger = _boot(journal)
  seed: Shorthand | None = None
  if shorthand:
    try:
      seed = parse_shorthand(shorthand)
    except ShorthandError as exc:
      console.print(f"[red]Niet opgeslagen: {exc}[/red]")
      return 2
    console.print(
      f"[dim]Shorthand: {seed.date or '(datum gevraagd)'}  "
      f"{seed.category or '(categorie gevraagd)'}  "
      f"{seed.description or '(omschrijving gevraagd)'}  "
      f"{seed.amount if seed.amount is not None else '(bedrag gevraagd)'}[/dim]"
    )
    iso_date = iso_date if iso_date is not None else seed.date
    description = description if description is not None else seed.description
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
      ledger,
      seed=seed
      if (iso_date is None or description is None or category is None or amount is None)
      else None,
    )
    if prompted is None:
      console.print("[yellow]Geannuleerd — niets opgeslagen.[/yellow]")
      return 2
    p_date, p_description, p_category, p_amount, _p_checking = prompted
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
    iso_date, description, category, amount = p_date, p_description, p_category, p_amount

  error = _validate(iso_date, description, category, amount or 0.0, ledger)
  if error:
    console.print(f"[red]Niet opgeslagen: {error}[/red]")
    return 2

  if dry_run:
    projected, _parent = projected_checking(ledger, iso_date, amount or 0.0)
    console.print("[yellow]Dry-run — niets opgeslagen. Zou toevoegen:[/yellow]")
    console.print(
      f"  {iso_date}  {description}  [{category}]  {amount:+,.2f} → checking {projected:,.2f}"
    )
    return 0

  mutation, entry = cmd_add(
    iso_date=iso_date,
    description=description.strip(),
    category=category,
    amount=amount or 0.0,
    journal=journal,
  )
  if entry is None:
    console.print("[red]Niet opgeslagen: onverwacht leeg resultaat.[/red]")
    return 2
  balances = entry.balances
  console.print(f"[green]Opgeslagen als {entry.id}[/green]")
  console.print(
    f"  {entry.date.isoformat()}  {entry.description}  [{entry.category}]  "
    f"{(entry.postings.get('checking') or 0.0):+,.2f} → checking "
    f"{balances.get('checking', 0.0):,.2f}, spaar {balances.get('savings', 0.0):,.2f}"
    f"  (status: actual)"
  )
  return 0
