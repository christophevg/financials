"""The journaled ledger's view layer (roadmap step 6): boot the committed
ledger (checkpoint + tail replay) and project it into the Transaction
shape the view/overlay code renders, plus the projection composition
(expected one-offs + recurrence expansions) and the `list` renderer.

Composition per the design: committed ledger ⊕ expected one-offs ⊕ rule
expansions. The projection is a VIEW: nothing here writes stores.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from financials.journal import Journal, Ledger, load_ledger
from financials.model import STATUS_ACTUAL, Transaction
from financials.recurrences import (
  RULE_LOOKBACK_DAYS,
  Recurrence,
  expand_recurrences,
  load_recurrences,
)

console = Console()

_FOOTER_MAX_MOMENTS = 5

_STRUCTURAL = ("Opening", "Saldocorrectie")


def command_journal() -> Journal:
  """The canonical journal. Promotion-aware: before the data promotion
  the migration staging journal is canonical (both files exist; the
  migration one holds the bootstrap chain); after promotion
  migration/journal.jsonl no longer exists and data/journal.jsonl is
  canonical. Resolved through fixes so tests can monkeypatch."""
  from financials.fixes import journal_file, migration_journal_file

  migration = migration_journal_file()
  if migration.exists():
    migration.parent.mkdir(parents=True, exist_ok=True)
    return Journal(migration)
  canonical = journal_file()
  canonical.parent.mkdir(parents=True, exist_ok=True)
  return Journal(canonical)


def load_ledger_view() -> Ledger:
  """Boot the committed ledger for a view command."""
  return load_ledger(command_journal())


def view_rows() -> list[Transaction]:
  """The committed ledger as view rows: every committed entry projected
  into the Transaction shape (postings -> amount; the ledger's computed
  balances become the balance columns). Raw category fields mirror the
  canonical category: the importer's mechanical normalization is not
  part of the committed model."""
  ledger = load_ledger_view()
  rows: list[Transaction] = []
  for transaction in ledger.transactions:
    # The amount IS the checking posting (a transfer's savings posting
    # is its mirror, never added into the amount).
    amount: float | None = transaction.postings.get("checking")
    rows.append(
      Transaction(
        id=transaction.id or "",
        date=transaction.date.isoformat(),
        description=transaction.description,
        category_raw=transaction.category,
        category=transaction.category,
        subcategory_raw="",
        subcategory="",
        amount_eur=amount,
        status=STATUS_ACTUAL,
        linked_id="",
        balance_checking=transaction.balances.get("checking"),
        balance_savings=transaction.balances.get("savings"),
        flags=[],
        source_line=0,
        note="",
      )
    )
  return rows


# --- Projection composition (moved from ledger.py at step 6) ------------


def _fmt(value: float | None) -> str:
  if value is None:
    return "—"
  return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_balance(value: float | None) -> str:
  """_fmt, but negative balances render red in the table (rich markup;
  stripped automatically when output is piped/captured)."""
  text = _fmt(value)
  if value is not None and value < 0:
    return f"[red]{text}[/red]"
  return text


def _compose_projection(
  transactions: list[Transaction],
  expected: list[Transaction],
  rules: list[Recurrence],
  window_start: date,
  window_end: date,
) -> list[Transaction]:
  """Expected one-offs plus active rule expansions for the window,
  superseded per period by real entries, sorted by (date, id). Expected
  rows have no lower bound: landed-but-unconfirmed and overdue entries
  stay in the projection until confirmed into the ledger."""
  virtual = expand_recurrences(rules, transactions, window_start, window_end)
  rows = [t for t in expected if t.date and date.fromisoformat(t.date) <= window_end]
  return sorted(rows + virtual, key=lambda t: (t.date, t.id))


def _walk(
  projection: list[Transaction],
  checking: float | None,
  savings: float | None,
) -> list[tuple[Transaction, float | None, float | None]]:
  """Running projected balances per entry: checking accumulates every
  entry; savings moves only on Overdracht rows (transfer out)."""
  walked: list[tuple[Transaction, float | None, float | None]] = []
  for t in projection:
    if checking is not None and t.amount_eur is not None:
      checking = round(checking + t.amount_eur, 2)
    if savings is not None and t.amount_eur is not None and t.category == "Overdracht":
      savings = round(savings - t.amount_eur, 2)
    walked.append((t, checking, savings))
  return walked


def _split_open(
  walk: list[tuple[Transaction, float | None, float | None]],
  today: date,
) -> tuple[
  list[tuple[Transaction, float | None, float | None]],
  list[tuple[Transaction, float | None, float | None]],
]:
  """The openstaand section (every walked row dated on or before today:
  overdue expected entries plus rule instances that landed between the
  ledger's anchor and today — landed but not yet confirmed into the
  ledger) and the future projection, both in walk (balance) order."""
  iso_today = today.isoformat()
  open_rows = [row for row in walk if row[0].date <= iso_today]
  future = [row for row in walk if row[0].date > iso_today]
  return open_rows, future


def _projection_walk(
  today: date, project: int = 1
) -> tuple[list[tuple[Transaction, float | None, float | None]], Transaction | None]:
  """The projection spine shared by `print_ledger_view` and `open_rows`:
  boot the committed ledger, seed from the anchor's balances, compose the
  overlay over a horizon of at least the year end (the footer's state_at
  cuts at month/year end; a 1-day `project` keeps the walk lean when the
  consumer only needs the open section) and return (walk, anchor)."""
  from financials.groups import load_groups
  from financials.model import load_expected

  rows = view_rows()
  committed = [row for row in rows if not row.description.startswith(_STRUCTURAL)]
  anchor = _anchor_row([t for t in committed if t.date <= today.isoformat()], today)
  start_checking = anchor.balance_checking if anchor else None
  start_savings = anchor.balance_savings if anchor else None

  # The projection reaches back past the ledger's anchor (the last
  # committed row) by a grace lookback: rule instances between the anchor
  # and today expand too, so a landed-but-unconfirmed instance is visible
  # (and superseded by the real entry once committed). The grace covers an
  # OUT-OF-ORDER commit — a later-dated row committed while an earlier
  # instance is still unconfirmed (e.g. a monthly rule on the 24th, a
  # 09-25 row committed first). No anchor -> grace from today.
  window_start = (
    date.fromisoformat(anchor.date) - timedelta(days=RULE_LOOKBACK_DAYS)
    if anchor
    else today - timedelta(days=RULE_LOOKBACK_DAYS)
  )
  year_end = date(today.year, 12, 31)
  horizon = max(today + timedelta(days=project), year_end)
  # The overlay composes against the FULL committed set (rules supersede
  # per period against real entries) but only committed dated rows.
  projection = _compose_projection(
    [t for t in committed if t.date],
    load_expected(),
    load_recurrences(),
    window_start,
    horizon,
  )
  collapsed = _collapse_groups(projection, load_groups())
  return _walk(collapsed, start_checking, start_savings), anchor


def _collapse_groups(
  projection: list[Transaction],
  groups,
) -> list[Transaction]:
  """Replace each group's UNCOMMITTED member rows with one rollup row at
  the rollup date (the group's own date when set, else its members' max
  date): the total hits the balance there — a credit-card statement's
  purchases land on the payment day, not on the individual purchase
  days. Committed members never appear in the projection (they render
  display-only from the actuals table); dangling member ids (dropped
  expected rows, edited rules) are skipped. The caller walks the
  collapsed set, so balances are consistent: no member impact before the
  rollup date, the group total on it."""
  from financials.groups import group_rollup

  if not any(g.members for g in groups):
    return projection
  member_ids = {m for g in groups for m in g.members}
  kept = [t for t in projection if t.id not in member_ids]
  rollups: list[Transaction] = []
  for group in groups:
    rows = [t for t in projection if t.id in set(group.members)]
    if not rows:
      continue  # nothing projected (all committed or all dangling)
    rollups.append(group_rollup(group, rows))
  collapsed = kept + rollups
  collapsed.sort(key=lambda t: (t.date, t.id))
  return collapsed


def open_rows(today: date | None = None) -> list[Transaction]:
  """The OPEN section's rows (the `confirm` picker's backlog): every
  projected row dated on or before today — overdue expected entries plus
  landed-but-unconfirmed rule instances. A view-level read: nothing
  writes stores."""
  walk, _anchor = _projection_walk(today or date.today())
  iso_today = (today or date.today()).isoformat()
  return [t for t, _c, _s in walk if t.date <= iso_today]


def _fmt_state(checking: float | None, savings: float | None) -> str:
  save = f" · spaar €{_fmt(savings)}" if savings is not None else ""
  return f"checking €{_fmt(checking)}{save}"


def _footer_lines(
  walk: list[tuple[Transaction, float | None, float | None]],
  start_checking: float | None,
  start_savings: float | None,
  today: date,
) -> list[str]:
  """Footer under the ledger table: projected balances at the end of the
  month and the end of the year, plus the moments where checking or
  savings dips below zero up to 31 December (each first dip plus the
  deepest point). Empty when no anchor balance exists to project from."""
  if start_checking is None:
    return []
  month_end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
  year_end = date(today.year, 12, 31)

  def state_at(cutoff: date) -> tuple[float | None, float | None]:
    check: float | None = start_checking
    save: float | None = start_savings
    for t, checking, savings in walk:
      if date.fromisoformat(t.date) > cutoff:
        break
      check, save = checking, savings
    return check, save

  lines = [
    f"Projectie per {month_end:%d-%m-%Y} (einde maand): {_fmt_state(*state_at(month_end))}",
    f"Projectie per {year_end:%d-%m-%Y} (einde jaar): {_fmt_state(*state_at(year_end))}",
  ]

  moments: dict[str, list[str]] = {"checking": [], "spaar": []}
  deepest: dict[str, tuple[date, float]] = {}
  last: dict[str, float | None] = {
    "checking": start_checking,
    "spaar": start_savings,
  }
  for t, checking, savings in walk:
    when = date.fromisoformat(t.date)
    if when > year_end:
      break
    for account, value in (("checking", checking), ("spaar", savings)):
      if value is None:
        continue
      known = deepest.get(account)
      if known is None or value < known[1]:
        deepest[account] = (when, value)
      prev = last[account]
      if prev is not None and prev >= 0 and value < 0:
        moments[account].append(f"{when:%d-%m} {t.description[:24]} {_fmt(value)}")
      last[account] = value

  rendered = []
  for account in ("checking", "spaar"):
    parts = moments[account][:_FOOTER_MAX_MOMENTS]
    if len(moments[account]) > _FOOTER_MAX_MOMENTS:
      parts.append(f"… nog {len(moments[account]) - _FOOTER_MAX_MOMENTS} momenten")
    dip = deepest.get(account)
    if dip and dip[1] < 0:
      parts.append(f"diepste punt {dip[0]:%d-%m} {_fmt(dip[1])}")
    if parts:
      rendered.append(f"Onder nul ({account}): " + " · ".join(parts))
  if not rendered:
    rendered = [f"Onder nul: geen momenten tot {year_end:%d-%m-%Y}."]
  return lines + rendered


def _anchor_row(rows: list[Transaction], today: date) -> Transaction | None:
  """Chronologically-last committed row on-or-before today (structural
  rows excluded) — the projection's seed. Within a date the chain-tip
  convention decides (chain_last's key): added rows (source_line=0) rank
  above imported rows; full ties resolve by list position (later row
  supersedes) — the ledger's list is chain-ordered."""
  committed = [
    t
    for t in rows
    if t.date and not t.description.startswith(_STRUCTURAL) and date.fromisoformat(t.date) <= today
  ]
  if not committed:
    return None
  return max(
    committed,
    key=lambda t: (date.fromisoformat(t.date), t.source_line or 10**9, committed.index(t)),
  )


def _render_actual_rows(rows: list[Transaction]) -> list[tuple]:
  """(id, date, description, category, change, checking, savings) for the
  actuals window: balances are the ledger's computed chain (first-class
  snapshot state), rendered directly."""
  return [
    (t.id, t.date, t.description, t.category, t.amount_eur, t.balance_checking, t.balance_savings)
    for t in rows
  ]


def _committed_rollups(committed: list[Transaction]):
  """Display-only rollup rows for groups whose members are all committed:
  (rollup_row, member_rows). The actuals table shows the single rollup
  instead of the member rows (the ledger itself is untouched); balances
  are the CHAIN-LAST member's (the total has already landed there) —
  member_rows stays in the caller's chain order (the ledger's list is
  chain-ordered; re-sorting by (date, id) would pick the wrong member's
  balance for same-date rows). Partially committed groups are skipped
  here: their committed members still show individually while the
  unconfirmed rest rolls up in OPEN/PROJECTIE."""
  from financials.groups import group_rollup, load_groups

  out = []
  for group in load_groups():
    member_rows = [t for t in committed if t.id in set(group.members)]
    if not member_rows or len(member_rows) != len(group.members):
      continue  # empty or partially committed/unresolvable: no historical rollup
    rollup = group_rollup(group, member_rows)
    rollup.status = STATUS_ACTUAL
    rollup.date = max(t.date for t in member_rows)
    rollup.balance_checking = member_rows[-1].balance_checking
    rollup.balance_savings = member_rows[-1].balance_savings
    out.append((rollup, member_rows))
  return out


@dataclass
class ViewRows:
  """The composed view as DATA (renderer-agnostic): the `list` renderer
  and the TUI consume the same composition — one composition, two
  renderers. `open`/`future`/`rollups` rows keep walk order; `open` and
  `future` rows are (Transaction, checking, savings) exactly as `_walk`
  emits them; `rollups` are (rollup_row, member_rows) as
  `_committed_rollups` emits them. Renderers derive section breaks from
  the empty-list checks, never from label rows."""
  history: list[Transaction]  # committed, OLDER than the actuals window (ascending)
  actuals: list[Transaction]
  rollups: list[tuple]
  open: list[tuple]
  future: list[tuple]
  footer: list[str]
  needle: str  # "" when unfiltered
  today: date
  days: int
  project: int


def build_view(
  days: int,
  project: int,
  filter: str | None = None,
  *,
  today: date | None = None,
  days_back: int | None = None,
  projection_horizon: date | None = None,
) -> ViewRows:
  """The `list` composition as pure data: the committed rows (journaled
  ledger), the projected walk, the open section, the future projection
  and the footer lines. No rendering — `print_ledger_view` and the TUI
  both consume this.

  Keyword-only knobs (the `list` renderer never passes them):
  `days_back` extends the actuals window start (`today − days_back` —
  the TUI's 3-day window); `projection_horizon` extends the projection
  cut (the TUI's year-end horizon — the footer already walks to year
  end, so extending the projection to it costs nothing).

  `filter` (grep): case-insensitive substring on description, category
  or id; a VIEW aid only — it limits which rows are shown in both
  windows (actuals + projection), while balances and the footer are
  always computed from the full unfiltered chain."""
  today = today or date.today()
  walk, anchor = _projection_walk(today, project=project)
  committed = [row for row in view_rows() if not row.description.startswith(_STRUCTURAL)]

  # The projection reaches back past the ledger's anchor (the last
  # committed row) by a grace lookback: rule instances between the anchor
  # and today expand too, so a landed-but-unconfirmed instance is visible
  # (and superseded by the real entry once committed). The grace covers an
  # OUT-OF-ORDER commit — a later-dated row committed while an earlier
  # instance is still unconfirmed (e.g. a monthly rule on the 24th, a
  # 09-25 row committed first). No anchor -> grace from today.
  window_end = projection_horizon or (today + timedelta(days=project))
  # The middle section: every uncommitted row dated on or before today
  # (overdue expected + landed rule instances); the rest is the projection,
  # cut at the --project window (the footer's walk runs to the year end).
  open_rows, future = _split_open(walk, today)
  cutoff = window_end.isoformat()
  future = [row for row in future if row[0].date <= cutoff]

  needle = (filter or "").strip().lower()

  def matches(row: Transaction) -> bool:
    if not needle:
      return True
    return (
      needle in row.description.lower()
      or needle in (row.category or "").lower()
      or needle in (row.id or "").lower()
    )

  open_rows = [(t, c, s) for t, c, s in open_rows if matches(t)]
  future = [(t, c, s) for t, c, s in future if matches(t)]
  window_days = days_back if days_back is not None else days
  window_start = today - timedelta(days=window_days)
  # Display-only historical rollups: groups whose members are ALL committed
  # collapse into one row (the ledger is untouched). Seating (below) walks
  # the FULL committed chain in order: when the chain index hits a
  # member's seat value, the rollup row is emitted INSTEAD of the chain
  # row — the rollup lands exactly at the chain position of its LAST
  # member (the point where the total landed), so whatever chained after
  # it (a Bankkosten committed after the members) renders below it,
  # exactly as the ledger orders it, and the balance column stays
  # seamless (the rollup's balance IS the seat member's, carried from
  # _committed_rollups). Non-seat members vanish (hidden_committed); a
  # needle-filtered member still holds its seat (the filter is a VIEW
  # aid — balances and position never move for a filter). The emit
  # decides its band by the EMITTED row's date (rollup date = max member
  # dates): before the window start → history (the TUI's band), else the
  # actuals window (both renderers consume them pre-seated; no renderer
  # placement loop anymore).
  fully_committed_rollups = _committed_rollups(committed)
  hidden_committed = {
    member.id for _rollup, members in fully_committed_rollups for member in members
  }
  committed_ids = {t.id: i for i, t in enumerate(committed)}
  seat_map: dict[int, Transaction] = {}
  for rollup, members in fully_committed_rollups:
    seat_map[committed_ids[members[-1].id]] = rollup
  seat_members = {members[-1].id for _rl, members in fully_committed_rollups}
  history: list[Transaction] = []
  actuals: list[Transaction] = []
  for i, row in enumerate(committed):
    if row.id in hidden_committed and row.id not in seat_members:
      continue  # a non-seat member: the rollup speaks for it
    if not matches(row) and i not in seat_map:
      continue  # needle-filtered non-seat row
    emit = seat_map.get(i, row)
    if date.fromisoformat(emit.date) < window_start:
      history.append(emit)
    else:
      actuals.append(emit)
  footer = _footer_lines(
    walk,
    anchor.balance_checking if anchor else None,
    anchor.balance_savings if anchor else None,
    today,
  )
  return ViewRows(
    history=history,
    actuals=actuals,
    rollups=[],
    open=open_rows,
    future=future,
    footer=footer,
    needle=needle,
    today=today,
    days=days,
    project=project,
  )


def print_ledger_view(days: int, project: int, filter: str | None = None) -> None:
  """`financials list` renderer: composes via build_view and renders the
  same table as before (identical output — the composition moved to
  build_view, the rendering stayed here)."""
  view = build_view(days, project, filter)
  needle = view.needle
  console.print(
    f"[bold]Kasboek[/bold] [dim]— journaal-geboekt; actuals: laatste {days} "
    f"dagen · openstaand: geland maar nog niet bevestigd (expected + regels "
    f"tot vandaag) · projectie: komende {project} dagen"
    + (f"; filter: '{needle}'" if needle else "")
    + "[/dim]"
  )
  table = Table(box=box.SIMPLE)
  table.add_column("Id", style="dim")
  table.add_column("Datum", justify="left")
  table.add_column("Omschrijving")
  table.add_column("Categorie")
  table.add_column("Verandering", justify="right")
  table.add_column("Checking", justify="right")
  table.add_column("Spaar", justify="right")

  for r in _render_actual_rows(view.actuals):
    table.add_row(r[0], r[1], r[2], r[3], f"{r[4]:+,.2f}", _fmt_balance(r[5]), _fmt_balance(r[6]))
  # Rollup rows are already seated inside view.actuals (at the last
  # member's chain position) — nothing to place here anymore.

  if view.open:
    table.add_section()
    # Label in the Omschrijving column: the widest one, so rich wraps at
    # word boundaries (never mid-word) on narrow terminals.
    table.add_row("", "", "OPEN", "", "", "", "")
    for t, checking, savings in view.open:
      table.add_row(
        t.id,
        t.date,
        t.description,
        t.category,
        f"{t.amount_eur:+,.2f}",
        _fmt_balance(checking),
        _fmt_balance(savings),
      )
  if view.future:
    table.add_section()
    table.add_row("", "", "PROJECTIE", "", "", "", "")
    for t, checking, savings in view.future:
      table.add_row(
        t.id,  # r:-hash ids are addressable: edit <r-id> works on them
        t.date,
        t.description,
        t.category,
        f"{t.amount_eur:+,.2f}",
        _fmt_balance(checking),
        _fmt_balance(savings),
        style="dim",
      )
  console.print(table)

  if not view.actuals and not view.open and not view.future and not view.rollups:
    if needle:
      console.print(f"[dim]Geen rijen die matchen '{needle}'.[/dim]")
    else:
      console.print("[dim]Geen rijen in deze vensters.[/dim]")

  if view.footer:
    console.print()
    for line in view.footer:
      console.print(f"[dim]{escape(line)}[/dim]")
