"""Groups: named rollups of projected/committed rows (financials group).

A group bundles related rows into one virtual row in the view — the
credit-card statement case: every purchase of one statement is a member
(`e####`, committed hash id, or `r:` rule instance) and the statement
shows as a single "Mastercard" row with the summed amount on the
payment date.

Groups are organization metadata, NOT money movement: they are not
journaled (journal = committed money; expected + rules + groups =
forecast inputs), they live in their own `groups.json` overlay, and
membership edits never touch the member rows themselves. The projection
is a VIEW: hiding members and rolling them up happens at render/walk
time, and a group's members keep their ids — `group add/remove` only
edits the membership list.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table

from financials.model import Transaction, find_entry, load_expected

console = Console()


def _groups_file() -> Path:
  """The groups store, resolved at CALL time through model.groups_file —
  the conftest/isolation choke point (a module-level import would bind
  the real store before tests patch it)."""
  from financials.model import groups_file

  return groups_file()


@dataclass
class Group:
  """A named rollup of rows. `date` is the rollup/payment date; ""
  means the rollup lands on the members' max date. `category` and
  `note` are display metadata for the rollup row."""

  id: str
  label: str
  date: str = ""  # ISO rollup date; "" = max(member dates)
  category: str = ""
  note: str = ""
  members: list[str] = field(default_factory=list)


def load_groups(path: Path | None = None) -> list[Group]:
  path = path or _groups_file()
  if not path.exists():
    return []
  with open(path, encoding="utf-8") as fh:
    rows = json.load(fh)
  return [Group(**row) for row in rows]


def save_groups(groups: list[Group], path: Path | None = None) -> None:
  path = path or _groups_file()
  path.parent.mkdir(parents=True, exist_ok=True)
  with open(path, "w", encoding="utf-8") as fh:
    json.dump([asdict(g) for g in groups], fh, ensure_ascii=False, indent=2)
    fh.write("\n")


def _next_id(groups: list[Group]) -> str:
  highest = 0
  for group in groups:
    if group.id.startswith("g") and group.id[1:].isdigit():
      highest = max(highest, int(group.id[1:]))
  return f"g{highest + 1:04d}"


def find_group(groups: list[Group], id_or_name: str) -> Group | None:
  """Exact id first, then unique id prefix, then the group NAME
  (case-insensitive exact, then unique case-insensitive prefix) —
  `group add Mastercard e0016` addresses the group by name. Ambiguous
  prefixes (two groups 'Mastercard september'/'Mastercard oktober') match
  nothing: ids stay the canonical address."""
  needle = (id_or_name or "").strip()
  if not needle:
    return None
  exact = next((g for g in groups if g.id == needle), None)
  if exact is not None:
    return exact
  hits = [g for g in groups if g.id.startswith(needle)]
  if len(hits) == 1:
    return hits[0]
  lowered = needle.lower()
  by_label = [g for g in groups if g.label.lower() == lowered]
  if len(by_label) == 1:
    return by_label[0]
  by_prefix = [g for g in groups if g.label.lower().startswith(lowered)]
  return by_prefix[0] if len(by_prefix) == 1 else None


def resolve_member(member_id: str) -> Transaction | None:
  """Resolve a member id across the three id families, in the resolver
  chain's order: committed (ledger) -> expected (e####) -> rule
  instance (r:-hash). Rule instances project as virtual rows."""
  from financials.commands import command_journal
  from financials.journal import load_ledger
  from financials.recurrence_cli import _find_instance
  from financials.recurrences import instance_id

  ledger = load_ledger(command_journal())
  entry, _ = ledger.find(member_id)
  if entry is not None:
    return Transaction(
      id=entry.id or "",
      date=entry.date.isoformat(),
      description=entry.description,
      category_raw=entry.category,
      category=entry.category,
      amount_eur=entry.postings.get("checking"),
      status="actual",
      balance_checking=entry.balances.get("checking"),
      balance_savings=entry.balances.get("savings"),
    )
  expected = find_entry(member_id, [], load_expected())
  if expected is not None:
    return expected
  hit = _find_instance(member_id)
  if hit is not None:
    rule_hit, occurrence = hit
    return Transaction(
      id=member_id,
      date=occurrence.isoformat(),
      description=rule_hit.description,
      category_raw=rule_hit.category,
      category=rule_hit.category,
      amount_eur=rule_hit.amount,
      status="expected",
    )
  # Last resort: a FUTURE instance of a rule edited since the id was
  # minted (the id is content-hash; an edit re-mints) — find any active
  # rule whose expansion still produces this id.
  for rule in _active_rules():
    for when in _rule_dates(rule):
      if instance_id(rule, when) == member_id:
        return Transaction(
          id=member_id,
          date=when.isoformat(),
          description=rule.description,
          category_raw=rule.category,
          category=rule.category,
          amount_eur=rule.amount,
          status="expected",
        )
  return None


def _active_rules():
  from financials.recurrences import load_recurrences

  return [r for r in load_recurrences() if r.active]


def _rule_dates(rule) -> list[date]:
  """All expansion dates of an active rule over a wide window (past
  60 days through +400 days — the same breadth as the view's walk plus
  the instance resolver's future reach)."""
  from financials.recurrences import RULE_LOOKBACK_DAYS, _instances_for

  today = date.today()
  return _instances_for(
    rule,
    today - timedelta(days=RULE_LOOKBACK_DAYS + 60),
    today + timedelta(days=400),
  )


# --- CLI ------------------------------------------------------------------


def rollup_date(group: Group, rows: list[Transaction]) -> date:
  """The group's rollup date: its own ISO date when set (the payment
  date), else the members' max date."""
  if group.date:
    return date.fromisoformat(group.date)
  return max(date.fromisoformat(t.date) for t in rows if t.date)


def group_rollup(group: Group, rows: list[Transaction]) -> Transaction:
  """The virtual rollup row standing in for the group's members:
  id = the group id (g####), description = ⧉ label (count), amount =
  the member sum."""
  return Transaction(
    id=group.id,
    date=rollup_date(group, rows).isoformat(),
    description=f"⧉ {group.label} ({len(rows)})",
    category_raw=group.category,
    category=group.category,
    amount_eur=round(sum(t.amount_eur or 0.0 for t in rows), 2),
    status=rows[0].status,
    note=f"groep: leden = {', '.join(t.id for t in rows)}",
  )


def _resolve_group(args_id: str) -> Group | None:
  groups = load_groups()
  group = find_group(groups, args_id)
  if group is None:
    console.print(f"[red]Onbekende groep: {args_id}[/red]")
  return group


def _render_show(group: Group) -> int:
  rows: list[tuple[str, str, str, str, str]] = []
  total = 0.0
  dangling: list[str] = []
  for member_id in group.members:
    t = resolve_member(member_id)
    if t is None or t.amount_eur is None:
      dangling.append(member_id)
      continue
    total += t.amount_eur
    rows.append((member_id, t.date, t.description, t.category, f"{t.amount_eur:+,.2f}"))
  table = Table(title=f"{group.id} — {group.label}", box=box.SIMPLE)
  for column, justify in (
    ("Id", "left"),
    ("Datum", "left"),
    ("Omschrijving", "left"),
    ("Categorie", "left"),
    ("Bedrag", "right"),
  ):
    table.add_column(column, justify=justify)  # type: ignore[arg-type]
  for row in rows:
    table.add_row(*row)
  table.add_section()
  table.add_row("", "", "TOTAAL", group.category or "", f"{total:+,.2f}")
  console.print(table)
  if group.date:
    console.print(f"[dim]Rolldatum: {group.date}[/dim]")
  if group.note:
    console.print(f"[dim]Notitie: {group.note}[/dim]")
  if dangling:
    console.print(
      "[yellow]Niet meer resolveerbaar (dangelend): " + ", ".join(dangling) + "[/yellow]"
    )
  return 0 if rows or not group.members else 2


def _ask(label: str, default: str = "") -> str:
  from rich.prompt import Prompt

  return Prompt.ask(label, default=default).strip()


def create_group(
  label: str | None,
  date_text: str = "",
  category: str = "",
  note: str = "",
) -> int:
  """Create an empty group; members are added with `group add`."""
  label = (label or "").strip()
  if not label:
    label = _ask("Naam van de groep")
  if not label:
    console.print("[dim]Geannuleerd — niets gewijzigd.[/dim]")
    return 2
  groups = load_groups()
  group = Group(id=_next_id(groups), label=label, date=date_text, category=category, note=note)
  groups.append(group)
  save_groups(groups)
  console.print(f"[green]Aangemaakt: {group.id} — {group.label}[/green]")
  return 0


def add_member(group_id: str, *member_ids: str) -> int:
  """Add rows to a group's membership (the rows themselves are untouched).
  All-or-nothing: one unknown id -> nothing is added; already-members
  are idempotent no-ops (not errors)."""
  group = _resolve_group(group_id)
  if group is None:
    return 2
  groups = load_groups()
  group = next(g for g in groups if g.id == group.id)
  unknown = [m for m in member_ids if resolve_member(m) is None]
  if unknown:
    console.print(
      "[red]Onbekende id's (niets toegevoegd): " + ", ".join(unknown) + "[/red]"
    )
    return 2
  fresh = [m for m in member_ids if m not in group.members]
  if not fresh:
    console.print(f"[dim]Alles zit al in {group.id}.[/dim]")
    return 0
  group.members.extend(fresh)
  save_groups(groups)
  console.print(f"[green]Toegevoegd: {', '.join(fresh)} → {group.id} ({group.label})[/green]")
  return 0


def remove_member(group_id: str, *member_ids: str) -> int:
  """Remove rows from a group's membership: un-group only, the rows
  themselves survive in their own stores. Idempotent on non-members."""
  group = _resolve_group(group_id)
  if group is None:
    return 2
  groups = load_groups()
  group = next(g for g in groups if g.id == group.id)
  present = [m for m in member_ids if m in group.members]
  if not present:
    console.print(f"[dim]Niets te verwijderen in {group.id}.[/dim]")
    return 0
  group.members = [m for m in group.members if m not in present]
  save_groups(groups)
  console.print(f"[green]Verwijderd uit groep: {', '.join(present)} ← {group.id}[/green]")
  return 0


def delete_group(group_id: str) -> int:
  """Delete the group; members survive untouched (un-group, not delete)."""
  group = _resolve_group(group_id)
  if group is None:
    return 2
  from rich.prompt import Confirm

  if not Confirm.ask(f"Groep {group.id} ({group.label}) verwijderen?", default=False):
    console.print("[dim]Geannuleerd — niets gewijzigd.[/dim]")
    return 2
  groups = load_groups()
  save_groups([g for g in groups if g.id != group.id])
  console.print(f"[green]Groep verwijderd: {group.id} — leden blijven bestaan.[/green]")
  return 0


def edit_group(group_id: str) -> int:
  """Interactive edit: label, rollup date, category, note (Enter keeps)."""
  group = _resolve_group(group_id)
  if group is None:
    return 2
  console.print(
    f"[bold]Groep {group.id} bewerken[/bold] [dim](Enter = houden, 'q' = annuleren)[/dim]"
  )
  label = _ask("Naam", group.label)
  if label == "q":
    console.print("[dim]Geannuleerd — niets gewijzigd.[/dim]")
    return 2
  date_text = _ask("Rolldatum (YYYY-MM-DD, leeg = datum van het laatste lid)", group.date)
  if date_text == "q":
    console.print("[dim]Geannuleerd — niets gewijzigd.[/dim]")
    return 2
  category = _ask("Categorie", group.category)
  if category == "q":
    console.print("[dim]Geannuleerd — niets gewijzigd.[/dim]")
    return 2
  note = _ask("Notitie", group.note)
  if note == "q":
    console.print("[dim]Geannuleerd — niets gewijzigd.[/dim]")
    return 2
  groups = load_groups()
  target = next(g for g in groups if g.id == group.id)
  target.label = label or target.label
  target.date = date_text.strip()
  target.category = category.strip()
  target.note = note.strip()
  save_groups(groups)
  console.print(f"[green]Bijgewerkt: {target.id}[/green]")
  return 0


def list_groups() -> int:
  groups = load_groups()
  if not groups:
    console.print("[dim]Geen groepen.[/dim]")
    return 0
  table = Table(box=box.SIMPLE)
  for column, justify in (
    ("Id", "left"),
    ("Naam", "left"),
    ("Rolldatum", "left"),
    ("Leden", "right"),
  ):
    table.add_column(column, justify=justify)  # type: ignore[arg-type]
  for group in groups:
    table.add_row(group.id, group.label, group.date or "(leden)", str(len(group.members)))
  console.print(table)
  return 0


def show_group(group_id: str) -> int:
  """Drill-down: every member with its resolved row + the running total."""
  group = _resolve_group(group_id)
  if group is None:
    return 2
  return _render_show(group)
