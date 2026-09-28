"""CLI for recurring rules (financials recurrence) — phase 7.

The engine (recurrences.py) stays pure storage/expansion; this module is
the command surface. Rules are the recurring analogue of the expected
register — overlay/config, NOT journaled: the journal is the story of
committed money only (owner decision, 2026-09-21).

Commands (wired in cli.py):
  recurrence list                      all rules + next projected instances
  recurrence add [...]                 create a rule (flags or prompted)
  recurrence pause/resume <prefix>     toggle a rule's active state
  recurrence remove <prefix>           delete a rule, with confirmation
  recurrence detect [--min N]          propose rules from history (review
                                       only; add turns one into a rule)
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from rich import box
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from financials.ask import ABORT, ask_category
from financials.commands import command_journal
from financials.recurrences import (
  AMOUNT_TOLERANCE,
  DAY_WINDOW,
  RULE_LOOKBACK_DAYS,
  Recurrence,
  _instances_for,
  detect_candidates,
  load_recurrences,
  save_recurrences,
)

console = Console()

RETRY_LIMIT = 3


# --- helpers -------------------------------------------------------------


def _resolve_rule(prefix: str, rules: list[Recurrence]) -> Recurrence | None:
  """Unambiguous description prefix (case-insensitive) -> the rule.
  Ambiguity prints the numbered candidates; None means unresolved
  (absent, ambiguous-but-declined, or empty prefix)."""
  needle = (prefix or "").strip().lower()
  if not needle:
    return None
  matches = [r for r in rules if r.description.lower().startswith(needle)]
  if len(matches) == 1:
    return matches[0]
  if not matches:
    console.print(f"[red]Geen regel gevonden voor {prefix!r}.[/red]")
    return None
  console.print(f"[yellow]{prefix!r} is niet eenduidig — kies:[/yellow]")
  numbered = {str(i + 1): rule for i, rule in enumerate(matches)}
  for i, rule in enumerate(matches, 1):
    state = "" if rule.active else " [dim](gepauzeerd)[/dim]"
    console.print(f"  {i}. {rule.description} — {rule.amount:+,.2f}{state}")
  choice = Prompt.ask("Nummer (leeg = annuleren)", default="")
  if choice not in numbered:
    console.print("[dim]Geannuleerd.[/dim]")
    return None
  return numbered[choice]


def _next_instances(rule: Recurrence, count: int = 3) -> list[str]:
  """The next `count` projected instance dates from today on."""
  window_end = TODAY() + timedelta(days=400)
  instances: list[str] = []
  day = TODAY()
  while len(instances) < count:
    chunk = _instances_for(rule, day, window_end)
    if not chunk:
      break
    for instance in chunk:
      if (
        instance > TODAY()
        and instance.isoformat() not in rule.exceptions
        and (not rule.end or instance <= date.fromisoformat(rule.end))
      ):
        instances.append(instance.isoformat())
        if len(instances) == count:
          break
    day = window_end
  return instances


def TODAY() -> date:
  """Indirection for testability (monkeypatchable 'today')."""
  return date.today()


_WEEKDAY_ALIASES: dict[str, int] = {
  # Dutch + English, 0 = Monday (date.weekday())
  "ma": 0, "di": 1, "wo": 2, "do": 3, "vr": 4, "za": 5, "zo": 6,
  "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

_WEEKDAY_NAMES = ["ma", "di", "wo", "do", "vr", "za", "zo"]


def _snap_to_weekday(anchor: str, weekday_text: str) -> str | None:
  """Snap an ISO anchor date forward to the given weekday (today when the
  anchor is empty). A bare weekday name returns the next such weekday on
  or after the anchor; an ISO date passes through unchanged (its own
  weekday IS the anchor). None when unparseable."""
  base = anchor or TODAY().isoformat()
  try:
    base_date = date.fromisoformat(base)
  except ValueError:
    return None
  needle = (weekday_text or "").strip().lower()
  if not needle:
    return base  # keep the anchor's own weekday
  offset = _WEEKDAY_ALIASES.get(needle)
  if offset is None:
    return None  # not a weekday alias: caller keeps the base anchor
  delta = (offset - base_date.weekday()) % 7
  return (base_date + timedelta(days=delta)).isoformat()


def _parse_amount(text: str) -> float | None:
  """Dutch-friendly float parse (comma decimal); None when invalid."""
  try:
    value = round(float(text.strip().replace(",", ".")), 2)
  except ValueError:
    return None
  return value or None  # 0 is not allowed (same rule as expect/add)


def _validate(rule: Recurrence) -> str:
  """Empty string when the rule is valid; the rejection reason otherwise."""
  from financials.config import approved_categories

  if not rule.description.strip():
    return "omschrijving mag niet leeg zijn"
  if rule.category not in approved_categories():
    return f"categorie {rule.category!r} is niet goedgekeurd"
  if not rule.amount:
    return "bedrag 0 is niet toegelaten"
  if rule.frequency not in ("monthly", "yearly", "weekly", "biweekly"):
    return "frequency must be monthly|yearly|weekly|biweekly"
  if rule.day is not None and not (1 <= rule.day <= 31):
    return "dag moet 1..31 zijn"
  if rule.frequency == "yearly":
    if rule.month is None:
      return "yearly vereist --month (1..12)"
    if not (1 <= rule.month <= 12):
      return "maand moet 1..12 zijn"
  if rule.frequency in ("weekly", "biweekly") and not rule.start:
    return "weekly/biweekly vereisen --start YYYY-MM-DD (de weekdag-anker)"
  if rule.start:
    try:
      date.fromisoformat(rule.start)
    except ValueError:
      return f"ongeldige start-datum {rule.start!r}"
  if rule.end:
    try:
      end = date.fromisoformat(rule.end)
    except ValueError:
      return f"ongeldige end-datum {rule.end!r}"
    if rule.start and end < date.fromisoformat(rule.start):
      return "end-datum ligt vóór de start-datum"
  return ""


# --- list ----------------------------------------------------------------


def list_recurrences() -> int:
  rules = load_recurrences()
  if not rules:
    console.print("[dim]Geen terugkerende regels (data/recurrences.json bestaat nog niet).[/dim]")
    console.print(
      "[dim]Zet er één met 'financials recurrence add', of laat voorstellen "
      "zien met 'financials recurrence detect'.[/dim]"
    )
    return 0
  table = Table(title="Terugkerende regels", box=box.SIMPLE)
  columns: list[tuple[str, Literal["left", "right"]]] = [
    ("Omschrijving", "left"),
    ("Categorie", "left"),
    ("Bedrag", "right"),
    ("Frequentie", "left"),
    ("Vanaf", "left"),
    ("Tot", "left"),
    ("Status", "left"),
    ("Komende", "left"),
  ]
  for column, justify in columns:
    table.add_column(column, justify=justify)
  for rule in rules:
    if rule.frequency == "monthly":
      frequency = f"maandelijks (dag {rule.day or 1})"
    elif rule.frequency == "yearly":
      frequency = f"jaarlijks {rule.month or 1:02d}-{rule.day or 1:02d}"
    else:  # weekly / biweekly
      weekday = (
        _WEEKDAY_NAMES[date.fromisoformat(rule.start).weekday()] if rule.start else "?"
      )
      prefix = "wekelijks" if rule.frequency == "weekly" else "tweewekelijks"
      frequency = f"{prefix} ({weekday})"
    upcoming = ", ".join(_next_instances(rule, count=3)) or "—"
    state = "actief" if rule.active else "[dim]gepauzeerd[/dim]"
    table.add_row(
      rule.description,
      rule.category,
      f"{rule.amount:+,.2f}",
      frequency,
      rule.start or "—",
      rule.end or "—",
      state,
      upcoming,
    )
  console.print(table)
  paused = sum(1 for rule in rules if not rule.active)
  console.print(
    f"[dim]{len(rules)} regel(s), {paused} gepauzeerd; superseding: "
    f"een echte transactie binnen ±{DAY_WINDOW} dagen rond de dag, "
    "zelfde categorie en bedrag, vervangt de instantie.[/dim]"
  )
  return 0


# --- add -----------------------------------------------------------------


def _prompt_missing(description: str | None, category: str | None, amount: float | None,
                    frequency: str | None) -> tuple[str, str, float, str] | None:
  """Fill missing add-fields interactively. Returns (description, category,
  amount, frequency) or None on abort. Fully-flagged invocations never
  prompt (non-interactive use stays non-interactive)."""
  if None not in (description, category, amount, frequency):
    assert description is not None and category is not None
    assert amount is not None and frequency is not None
    return description, category, amount, frequency
  console.print("[bold]Nieuwe regel[/bold] [dim](leeg = standaardwaarde, 'q' = annuleren)[/dim]")
  if description is None:
    description = Prompt.ask("Omschrijving", default="")
    if description == "q":
      return None
  if category is None:
    category = ask_category()
    if category == ABORT or category is None:
      return None
  if amount is None:
    amount_text = Prompt.ask("Bedrag (positief = inkomsten, negatief = uitgaven)")
    if amount_text == "q":
      return None
    amount = _parse_amount(amount_text)
    if amount is None:
      console.print("[red]Ongeldig bedrag (0 is niet toegelaten).[/red]")
      return None
  if frequency is None:
    frequency = Prompt.ask(
      "Frequentie (monthly/yearly/weekly/biweekly)", default="monthly"
    )
    if frequency == "q":
      return None
    frequency = frequency.strip().lower() or "monthly"
  return description, category, amount, frequency


def _seeded_field_loop(
  title: str,
  description: str,
  category: str,
  amount: float,
  frequency: str,
  day: int | None,
  month: int | None,
  start: str,
  end: str,
) -> Recurrence | None:
  """The seeded interactive loop: each field shows its current value;
  Enter keeps it, typing replaces it, 'q' aborts (returns None). Used by
  add-from-proposal (--take), rule edit, and occurrence-edit — the
  explicit-approval gate for detected and existing values."""
  console.print(f"[bold]{title}[/bold] [dim](Enter = houden, 'q' = annuleren)[/dim]")
  new_description = Prompt.ask("Omschrijving", default=description)
  if new_description == "q":
    return None
  new_category = ask_category(current=category)
  if new_category == ABORT or new_category is None:
    return None
  amount_text = Prompt.ask("Bedrag", default=f"{amount:g}")
  if amount_text == "q":
    return None
  new_amount = _parse_amount(amount_text)
  if new_amount is None:
    console.print("[red]Ongeldig bedrag (0 is niet toegelaten).[/red]")
    return None
  new_frequency = Prompt.ask(
    "Frequentie (monthly/yearly/weekly/biweekly)", default=frequency
  )
  if new_frequency == "q":
    return None
  new_frequency = new_frequency.strip().lower() or frequency
  if new_frequency in ("weekly", "biweekly"):
    # The anchor question REPLACES day-of-month for weekly/biweekly:
    # instances anchor on start (its weekday is the schedule); typing a
    # bare weekday name (ma/di/wo/do/vr/za/zo or mon..sun) snaps start
    # forward to that weekday; an ISO date becomes the anchor directly.
    anchor_text = Prompt.ask(
      "Weekdag (ma/di/wo/do/vr/za/zo, Enter = houd start)", default=""
    )
    if anchor_text == "q":
      return None
    new_day = day  # meaningless for weekly/biweekly; kept as-is
  else:
    day_text = Prompt.ask("Dag van de maand", default=str(day or 1))
    if day_text == "q":
      return None
    new_day = int(day_text) if day_text.strip().isdigit() else day
  new_month = month
  if new_frequency == "yearly":
    month_text = Prompt.ask("Maand (1-12)", default=str(month or 1))
    if month_text == "q":
      return None
    new_month = int(month_text) if month_text.strip().isdigit() else month
  start_text = Prompt.ask(
    "Start-datum (ISO, leeg = huidige"
    + (" — de weekdag-anker" if new_frequency in ("weekly", "biweekly") else "")
    + ")",
    default=start or "",
  )
  if start_text == "q":
    return None
  new_start = start_text.strip() or start
  if new_frequency in ("weekly", "biweekly"):
    new_start = _snap_to_weekday(new_start, anchor_text) or new_start
  end_text = Prompt.ask("End-datum (ISO, leeg = geen)", default=end or "")
  if end_text == "q":
    return None
  return Recurrence(
    description=new_description.strip() or description,
    category=new_category,
    amount=new_amount,
    frequency=new_frequency.strip() or frequency,
    day=new_day,
    month=new_month,
    start=new_start,
    end=end_text.strip(),
  )


def add_recurrence(
  description: str | None = None,
  category: str | None = None,
  amount: float | None = None,
  frequency: str | None = None,
  day: int | None = None,
  month: int | None = None,
  start: str | None = None,
  end: str = "",
  weekday: str | None = None,
) -> int:
  """Create a rule. Returns 0 on success, 2 on rejection/abort.

  The interactive path (any core field missing) walks the same question
  set as the seeded loop: day of month for monthly/yearly, month for
  yearly, weekday anchor for weekly/biweekly, then start and end date.
  Fully-flagged invocations never prompt — --day / --month / --weekday /
  --start / --end remain the non-interactive route (a flag surfaces as
  the default of its question on a partial-flag interactive run).
  """
  interactive = None in (description, category, amount, frequency)
  filled = _prompt_missing(description, category, amount, frequency)
  if filled is None:
    return 2
  description, category, amount, frequency = filled
  if interactive and day is None and frequency in ("monthly", "yearly"):
    # Gap fix: interactive add silently took TODAY().day as day-of-month
    # (only the seeded loop asked); plain add now asks too. Enter keeps
    # the old silent default (today's day).
    day_text = Prompt.ask("Dag van de maand", default=str(TODAY().day))
    if day_text == "q":
      return 2
    day = int(day_text.strip()) if day_text.strip().isdigit() else None
    if day is None or not 1 <= day <= 31:
      console.print("[red]Ongeldige dag (1-31).[/red]")
      return 2
  if day is None:
    day = TODAY().day
  if start is None:
    start = TODAY().isoformat()
  anchor_text = ""
  if frequency in ("weekly", "biweekly"):
    # CLI --weekday flag wins when given; the interactive path asks for
    # the anchor (Enter keeps the start's own weekday). The snap applies
    # AFTER the start question below, so a typed start is snapped too
    # (the seeded loop's order).
    if weekday:
      start = _snap_to_weekday(start, weekday_text=weekday) or start
    elif interactive:
      anchor_text = Prompt.ask(
        "Weekdag (ma/di/wo/do/vr/za/zo, Enter = houd start)", default=""
      )
      if anchor_text == "q":
        return 2
  if frequency == "yearly" and interactive and month is None:
    # Gap fix: interactive add with 'yearly' used to hard-fail with
    # "yearly vereist --month" — nothing asked for the month (the seeded
    # loop asks it for --take/edit; plain add now does too). Default is
    # today's month, mirroring day defaulting to today's day-of-month.
    month_text = Prompt.ask("Maand (1-12)", default=str(TODAY().month))
    if month_text == "q":
      return 2
    month = int(month_text.strip()) if month_text.strip().isdigit() else None
    if month is None or not 1 <= month <= 12:
      console.print("[red]Ongeldige maand (1-12).[/red]")
      return 2
  if interactive:
    # Seeded-loop parity for start/end: interactive add used to default
    # both silently (start = today, end = none). Enter keeps the
    # default, a typed ISO date replaces it, 'q' aborts.
    start_text = Prompt.ask(
      "Start-datum (ISO, leeg = huidige"
      + (" — de weekdag-anker" if frequency in ("weekly", "biweekly") else "")
      + ")",
      default=start,
    )
    if start_text == "q":
      return 2
    start = start_text.strip() or start
    if frequency in ("weekly", "biweekly"):
      start = _snap_to_weekday(start, anchor_text) or start
    end_text = Prompt.ask("End-datum (ISO, leeg = geen)", default=end or "")
    if end_text == "q":
      return 2
    end = end_text.strip()
  rule = Recurrence(
    description=description.strip(),
    category=category,
    amount=amount,
    frequency=frequency,
    day=day,
    month=month,
    start=start,
    end=end,
  )
  problem = _validate(rule)
  if problem:
    console.print(f"[red]Niet opgeslagen: {problem}.[/red]")
    return 2

  # Day-of-month sanity warning (day 29-31 skips short months).
  if rule.day is not None and rule.day > 28 and rule.frequency in ("monthly", "yearly"):
    console.print(
      f"[dim]Let op: dag {rule.day} bestaat niet in elke maand; die maanden "
      "springen de instantie over (volgende maand hervat het).[/dim]"
    )

  rules = load_recurrences()
  rules.append(rule)
  save_recurrences(rules)
  console.print(f"[green]Regel opgeslagen: {rule.description}[/green]")
  upcoming = ", ".join(_next_instances(rule, count=3)) or "—"
  if rule.frequency == "weekly":
    cadence = f"wekelijks (({_WEEKDAY_NAMES[date.fromisoformat(rule.start).weekday()]}))"
  elif rule.frequency == "biweekly":
    cadence = f"tweewekelijks ({_WEEKDAY_NAMES[date.fromisoformat(rule.start).weekday()]})"
  else:
    cadence = (
      f"{rule.frequency} (dag {rule.day or 1}"
      + (f", maand {rule.month}" if rule.frequency == "yearly" else "")
      + ")"
    )
  console.print(
    f"  {cadence} {rule.amount:+,.2f} [{rule.category}], vanaf {rule.start or 'vandaag'}"
  )
  console.print(f"  eerstvolgende: {upcoming}")
  console.print(
    "[dim]De instanties verschijnen in 'financials list' zodra het venster ze bevat.[/dim]"
  )
  return 0


# --- pause / resume / remove ---------------------------------------------


def _toggle(prefix: str, active: bool, verb: str) -> int:
  rules = load_recurrences()
  rule = _resolve_rule(prefix, rules)
  if rule is None:
    return 2
  if rule.active == active:
    state = "al actief" if active else "al gepauzeerd"
    console.print(f"[yellow]{rule.description} is {state}.[/yellow]")
    return 0
  rule.active = active
  save_recurrences(rules)
  console.print(f"[green]{verb}: {rule.description}[/green]")
  return 0


def pause_recurrence(prefix: str) -> int:
  return _toggle(prefix, active=False, verb="Gepauzeerd")


def resume_recurrence(prefix: str) -> int:
  return _toggle(prefix, active=True, verb="Hervat")


def remove_recurrence(prefix: str) -> int:
  rules = load_recurrences()
  rule = _resolve_rule(prefix, rules)
  if rule is None:
    return 2
  console.print(
    f"Verwijderen: {rule.description} — {rule.amount:+,.2f} [{rule.category}], "
    f"{rule.frequency} vanaf {rule.start or '?'}"
  )
  answer = Prompt.ask("Type 'ja' om te bevestigen", default="")
  if answer.strip().lower() != "ja":
    console.print("[dim]Geannuleerd — niets verwijderd.[/dim]")
    return 0
  save_recurrences([r for r in rules if r is not rule])
  console.print(f"[green]Verwijderd: {rule.description}[/green]")
  return 0


# --- edit (rule or single occurrence) ------------------------------------


def edit_recurrence(prefix: str) -> int:
  """Edit a rule by (prefix of) description — the seeded field loop;
  Enter keeps a value, typing replaces it."""
  rules = load_recurrences()
  rule = _resolve_rule(prefix, rules)
  if rule is None:
    return 2
  edited = _seeded_field_loop(
    f"Regel bewerken: {rule.description}",
    rule.description,
    rule.category,
    rule.amount,
    rule.frequency,
    rule.day,
    rule.month,
    rule.start,
    rule.end,
  )
  if edited is None:
    console.print("[dim]Geannuleerd — niets gewijzigd.[/dim]")
    return 2
  problem = _validate(edited)
  if problem:
    console.print(f"[red]Niet gewijzigd: {problem}.[/red]")
    return 2
  rules[rules.index(rule)] = edited
  save_recurrences(rules)
  console.print(f"[green]Regel gewijzigd: {edited.description}[/green]")
  upcoming = ", ".join(_next_instances(edited, count=3)) or "—"
  console.print(f"  eerstvolgende: {upcoming}")
  return 0


def _find_instance(entry_id: str) -> tuple[Recurrence, date] | None:
  """Resolve an r:-hash id to (rule, occurrence) among the ACTIVE rules'
  projected instances in [anchor − grace, +400d] — the same window as the
  view's open section, so landed-but-unconfirmed instances (including ones
  before an out-of-order-committed anchor) stay resolvable; no anchor
  falls back to the grace window from today. None = not a rule instance.
  Suppressed instances stay resolvable: expansion skips their dates, the
  dates themselves still exist."""
  from financials.ledger_view import _anchor_row, view_rows
  from financials.recurrences import _instances_for, instance_id

  today = TODAY()
  anchor = _anchor_row(view_rows(), today)
  window_start = (
    date.fromisoformat(anchor.date) - timedelta(days=RULE_LOOKBACK_DAYS)
    if anchor
    else today - timedelta(days=RULE_LOOKBACK_DAYS)
  )
  for rule in load_recurrences():
    if not rule.active:
      continue
    for occurrence in _instances_for(rule, window_start, today + timedelta(days=400)):
      if instance_id(rule, occurrence) == entry_id:
        return rule, occurrence
  return None


def commit_instance(
  rule_hit: Recurrence,
  occurrence_date: date,
  confirmed_date: date,
  description: str,
  category: str,
  amount: float,
) -> int:
  """The no-prompt commit core shared by `_confirm_instance` (the values
  come from the prefilled prompts) and `confirm.py` (the values come from
  the rule itself): one AddMutation — the journal records committed money;
  the instance is virtual. When the committed row does NOT cover the
  rule's period (drifted amount or out-of-window date), the occurrence is
  recorded as an exception so the unconfirmed instance doesn't linger in
  OPEN; a covering commit supersedes it automatically (nothing to
  record)."""
  from financials.commands import cmd_add

  iso = occurrence_date.isoformat()
  journal = command_journal()
  _mutation, entry = cmd_add(
    iso_date=confirmed_date.isoformat(),
    description=description,
    category=category,
    amount=amount,
    journal=journal,
  )
  if entry is None:
    console.print("[red]Niet bevestigd: de boeking kon niet worden toegepast.[/red]")
    return 2
  # No ghost instance: a commit that covers the rule's period supersedes
  # it in the projection; a drifted commit (amount/date outside the
  # superseding window) is excepted so the instance leaves OPEN.
  covered = (
    category == rule_hit.category
    and abs(amount - rule_hit.amount) <= AMOUNT_TOLERANCE
    and abs((confirmed_date - occurrence_date).days) <= DAY_WINDOW
  )
  if not covered:
    rules = load_recurrences()
    for rule in rules:
      if rule.description == rule_hit.description and rule.category == rule_hit.category:
        rule.exceptions = sorted(set(rule.exceptions) | {iso})
    save_recurrences(rules)
  balances = entry.balances
  console.print(
    f"[green]Bevestigd: {iso} → {entry.id}[/green]  [dim]checking "
    f"{balances.get('checking', 0.0):,.2f}, spaar "
    f"{balances.get('savings', 0.0):,.2f}[/dim]"
  )
  return 0


def _confirm_instance(rule_hit: Recurrence, occurrence_date: date) -> int:
  """Confirm a landed rule instance as a real transaction: prompts prefilled
  with the instance's values (Enter keeps, 'q' aborts), then the shared
  commit core."""
  iso = occurrence_date.isoformat()
  console.print(
    f"[bold]Bevestigen van {rule_hit.description} ({iso})[/bold] "
    "[dim](Enter = houden, 'q' = annuleren)[/dim]"
  )
  date_text = Prompt.ask("Datum (YYYY-MM-DD)", default=iso)
  if date_text.strip() == "q":
    console.print("[dim]Geannuleerd — niets gewijzigd.[/dim]")
    return 2
  try:
    confirmed_date = date.fromisoformat(date_text.strip())
  except ValueError:
    console.print(f"[red]Ongeldige datum: {date_text!r}[/red]")
    return 2
  description = Prompt.ask("Omschrijving", default=rule_hit.description)
  if description.strip() == "q":
    console.print("[dim]Geannuleerd — niets gewijzigd.[/dim]")
    return 2
  category = ask_category(current=rule_hit.category)
  if category == ABORT or category is None:
    console.print("[dim]Geannuleerd — niets gewijzigd.[/dim]")
    return 2
  amount_text = Prompt.ask("Bedrag", default=f"{rule_hit.amount:.2f}")
  if amount_text.strip() == "q":
    console.print("[dim]Geannuleerd — niets gewijzigd.[/dim]")
    return 2
  try:
    amount = round(float(amount_text.replace(",", ".").strip()), 2)
  except ValueError:
    console.print(f"[red]Ongeldig bedrag: {amount_text!r}[/red]")
    return 2
  if amount == 0:
    console.print("[red]Bedrag 0 is niet toegelaten.[/red]")
    return 2

  return commit_instance(
    rule_hit, occurrence_date, confirmed_date, description.strip(), category, amount
  )

def edit_occurrence(entry_id: str) -> int:
  """Edit a projected rule instance by its r:-hash id. A LANDED instance
  (date <= today) offers: (1) edit the RULE or (2) confirm THIS instance
  as a real transaction (the owner's confirmation workflow for the view's
  OPEN section). A future instance keeps the original two-option menu:
  (1) the rule or (2) an expected exception for this occurrence. Returns
  2 when the id is not a rule instance (the callers' own resolvers handle
  committed/expected ids first)."""
  hit = _find_instance(entry_id)
  if hit is None:
    return 2  # not a projected instance id
  rule_hit, occurrence_date = hit

  console.print(
    f"Instantie van regel: {rule_hit.description} — {rule_hit.amount:+,.2f} "
    f"[{rule_hit.category}] op {occurrence_date.isoformat()}"
  )
  if occurrence_date <= TODAY():
    choice = Prompt.ask(
      "Bewerken: (1) de regel  (2) deze instantie bevestigen als actual", default=""
    )
    if choice == "2":
      return _confirm_instance(rule_hit, occurrence_date)
    if choice == "1":
      return edit_recurrence(rule_hit.description)
    console.print("[dim]Geannuleerd — niets gewijzigd.[/dim]")
    return 0
  choice = Prompt.ask("Bewerken: (1) de regel  (2) alleen deze instantie", default="")
  if choice == "1":
    return edit_recurrence(rule_hit.description)
  if choice == "2":
    edited = _seeded_field_loop(
      f"Uitzondering op {rule_hit.description} ({occurrence_date.isoformat()})",
      rule_hit.description,
      rule_hit.category,
      rule_hit.amount,
      rule_hit.frequency,
      occurrence_date.day,
      rule_hit.month,
      occurrence_date.isoformat(),
      "",
    )
    if edited is None:
      console.print("[dim]Geannuleerd — niets gewijzigd.[/dim]")
      return 2
    from financials.expected import add_expected

    created = add_expected(
      iso_date=edited.start or occurrence_date.isoformat(),
      description=edited.description,
      category=edited.category,
      amount=edited.amount,
    )
    if created != 0:
      return created
    # exempt the original instance: only the exception shows now.
    # Re-LOAD the rules fresh: rule_hit belongs to the scan's list; a
    # second load_recurrences() call yields distinct objects, so identity
    # (rule is rule_hit) would never fire — match on content instead.
    rules = load_recurrences()
    for rule in rules:
      if rule.description == rule_hit.description and rule.category == rule_hit.category:
        rule.exceptions = sorted(set(rule.exceptions) | {occurrence_date.isoformat()})
    save_recurrences(rules)
    console.print(
      "[green]Instantie vervangen door een expected-uitzondering; de regel "
      "blijft de overige maanden draaien.[/green]"
    )
    return 0
  console.print("[dim]Geannuleerd — niets gewijzigd.[/dim]")
  return 0


def suppress_occurrence(entry_id: str) -> int:
  """The delete-wired suppression: 'delete <r-id>' lands here. The
  occurrence's date joins the rule's exceptions (idempotent), so the
  instance disappears from the projection while the rule keeps running
  for the remaining periods. Returns 2 when the id is not a rule
  instance (delete's other resolvers own those)."""
  hit = _find_instance(entry_id)
  if hit is None:
    return 2  # not a projected instance id
  rule_hit, occurrence_date = hit
  console.print(
    f"Instantie van regel: {rule_hit.description} — {rule_hit.amount:+,.2f} "
    f"[{rule_hit.category}] op {occurrence_date.isoformat()}"
  )
  if occurrence_date.isoformat() in rule_hit.exceptions:
    console.print("[yellow]Deze instantie is al onderdrukt.[/yellow]")
    return 0
  answer = Prompt.ask("Type 'ja' om te bevestigen", default="")
  if answer.strip().lower() != "ja":
    console.print("[dim]Geannuleerd — niets gewijzigd.[/dim]")
    return 2
  # Re-load fresh (same content-match discipline as edit_occurrence's
  # exception path): rule_hit belongs to the scan's list.
  rules = load_recurrences()
  for rule in rules:
    if rule.description == rule_hit.description and rule.category == rule_hit.category:
      rule.exceptions = sorted(set(rule.exceptions) | {occurrence_date.isoformat()})
  save_recurrences(rules)
  console.print(
    f"[green]Instantie onderdrukt ({occurrence_date.isoformat()}); de regel "
    "blijft de overige periodes draaien.[/green]"
  )
  return 0

# --- detect --------------------------------------------------------------


def detect_recurrences(min_occurrences: int = 3) -> int:
  """Run the detector over the committed ledger's actual rows and print
  the proposals as a review table. NOTHING is created — approving a
  proposal is 'recurrence add' (pre-filled interactively)."""
  from financials.ledger_view import view_rows

  proposals = detect_candidates(
    view_rows(), min_occurrences=min_occurrences, existing_rules=load_recurrences()
  )
  if not proposals:
    console.print(
      f"[dim]Geen kandidaat-regels gevonden (min {min_occurrences} gelijke maanden nodig).[/dim]"
    )
    return 0
  table = Table(title=f"Kandidaat-regels (min {min_occurrences}x)", box=box.SIMPLE)
  columns: list[tuple[str, Literal["left", "right"]]] = [
    ("#", "right"),
    ("Omschrijving", "left"),
    ("Categorie", "left"),
    ("Bedrag", "right"),
    ("Frequentie", "left"),
    ("Dag/maand", "left"),
    ("Aantal", "right"),
    ("Eerst", "left"),
  ]
  for column, justify in columns:
    table.add_column(column, justify=justify)
  for i, proposal in enumerate(proposals, 1):
    if proposal["frequency"] == "yearly":
      spacing = f"jaarlijks {proposal['month']:02d}-{proposal['day']:02d}"
    elif proposal["frequency"] == "monthly":
      spacing = f"maandelijks dag {proposal['day']}"
    else:  # weekly / biweekly: anchor weekday from the phase anchor
      anchor = date.fromisoformat(proposal["start"])
      weekday = _WEEKDAY_NAMES[anchor.weekday()]
      prefix = "wekelijks" if proposal["frequency"] == "weekly" else "tweewekelijks"
      spacing = f"{prefix} ({weekday})"
    table.add_row(
      str(i),
      proposal["description"],
      proposal["category"],
      f"{proposal['amount']:+,.2f}"
      + (
        f" (var {proposal['amount_range'][0]:g}..{proposal['amount_range'][1]:g})"
        if proposal.get("amount_range")
        else ""
      ),
      proposal["frequency"],
      spacing,
      str(proposal["occurrences"]),
      proposal["start"],
    )
  console.print(table)
  console.print("[dim]Niets is aangemaakt. Een voorstel overnemen: 'financials recurrence add' "
                "— of met nummer: 'financials recurrence detect --take N'.[/dim]")
  return 0


def take_proposal(number: int, min_occurrences: int = 3) -> int:
  """Adopt detector proposal #number — through the SEEDED field loop:
  every field shows the detected value; Enter accepts it, typing replaces
  it, 'q' aborts. Nothing is saved until the loop completes (the explicit
  approval step; the proposal alone never creates a rule)."""
  from financials.ledger_view import view_rows

  proposals = detect_candidates(
    view_rows(), min_occurrences=min_occurrences, existing_rules=load_recurrences()
  )
  if not (1 <= number <= len(proposals)):
    console.print(f"[red]Geen voorstel nummer {number} (er zijn {len(proposals)}).[/red]")
    return 2
  proposal = proposals[number - 1]
  edited = _seeded_field_loop(
    f"Voorstel {number}: {proposal['description']}",
    proposal["description"],
    proposal["category"],
    proposal["amount"],
    proposal["frequency"],
    proposal.get("day"),
    proposal.get("month"),
    proposal.get("start", ""),
    "",
  )
  if edited is None:
    console.print("[dim]Geannuleerd — niets opgeslagen.[/dim]")
    return 2
  problem = _validate(edited)
  if problem:
    console.print(f"[red]Niet opgeslagen: {problem}.[/red]")
    return 2
  rules = load_recurrences()
  rules.append(edited)
  save_recurrences(rules)
  console.print(f"[green]Regel opgeslagen: {edited.description}[/green]")
  upcoming = ", ".join(_next_instances(edited, count=3)) or "—"
  console.print(f"  eerstvolgende: {upcoming}")
  return 0
