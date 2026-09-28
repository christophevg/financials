"""Tests for ledger projection helpers and the footer (synthetic data;
the helpers moved to ledger_view.py at step 6)."""

import io
from datetime import date

from rich.console import Console

from financials.commands import cmd_add
from financials.journal import Journal
from financials.ledger_view import (
  _anchor_row,
  _compose_projection,
  _footer_lines,
  _split_open,
  _walk,
)
from financials.model import STATUS_EXPECTED, Transaction, save_expected
from financials.recurrences import Recurrence, instance_id, save_recurrences


def _pinned_date(today: date) -> type[date]:
  """A date subclass with today() pinned (date.today is C-level; patching
  the attribute on the type itself is refused)."""

  class _Pinned(date):
    @classmethod
    def today(cls) -> date:
      return today

  return _Pinned


def _wide_console() -> Console:
  """A 200-column non-tty console: rich truncates wide tables at the
  terminal width (a label like 'Openstaand' folds mid-word on the 80-col
  test console), so assertions need a known-wide render."""
  return Console(width=200, file=io.StringIO(), legacy_windows=False)


def make_tx(
  id: str = "e0001",
  date_str: str = "2026-10-01",
  amount: float = -100.0,
  category: str = "Uitgaven",
  description: str = "Huur",
  status: str = "expected",
) -> Transaction:
  return Transaction(
    id=id,
    date=date_str,
    description=description,
    category=category,
    category_raw=category,
    subcategory_raw="",
    subcategory="",
    amount_eur=amount,
    status=status,
    linked_id="",
    balance_checking=None,
    balance_savings=None,
    flags=[],
    source_line=0,
    note="",
  )


def test_fmt_balance_positive_and_none_stay_plain():
  from financials.ledger_view import _fmt_balance

  assert _fmt_balance(1520.0) == "1.520,00"
  assert _fmt_balance(None) == "—"


def test_fmt_balance_negative_wraps_in_red_markup():
  from financials.ledger_view import _fmt_balance

  assert _fmt_balance(-85.1) == "[red]-85,10[/red]"
  # zero is not negative
  assert _fmt_balance(0.0) == "0,00"


def test_walk_accumulates_checking_and_savings():
  rows = [
    make_tx(id="e0001", date_str="2026-10-01", amount=-100.0),
    make_tx(id="e0002", date_str="2026-10-02", amount=2000.0, category="Inkomsten"),
    make_tx(id="e0003", date_str="2026-10-03", amount=-500.0, category="Overdracht"),
  ]
  walk = _walk(rows, 1000.0, 5000.0)
  assert [checking for _t, checking, _s in walk] == [900.0, 2900.0, 2400.0]
  # A negative Overdracht moves money INTO savings: savings - amount.
  assert [savings for _t, _c, savings in walk] == [5000.0, 5000.0, 5500.0]


def test_walk_without_start_stays_none():
  walk = _walk([make_tx()], None, None)
  assert walk[0][1] is None
  assert walk[0][2] is None


def test_compose_window_bounds():
  expected = [
    make_tx(id="e0001", date_str="2026-09-01"),  # overdue: included
    make_tx(id="e0002", date_str="2026-12-31"),  # boundary: included
    make_tx(id="e0003", date_str="2027-01-05"),  # beyond window: excluded
  ]
  rows = _compose_projection([], expected, [], date(2026, 9, 18), date(2026, 12, 31))
  assert [t.id for t in rows] == ["e0001", "e0002"]


def test_compose_expands_rule_and_supersedes_covered_instance():
  rule = Recurrence(
    description="Huur",
    category="Uitgaven",
    amount=-950.0,
    frequency="monthly",
    day=5,
    start="2026-01-01",
  )
  paid = make_tx(
    id="t0001",
    date_str="2026-10-06",
    amount=-950.0,
    category="Uitgaven",
    description="Huur",
    status="actual",
  )
  rows = _compose_projection([paid], [], [rule], date(2026, 10, 1), date(2026, 11, 30))
  # October's instance is covered by the real transaction; November's is not.
  assert len(rows) == 1
  # virtual rows carry content-hash ids with the r: provenance prefix
  assert rows[0].id.startswith("r:") and len(rows[0].id) == 18
  assert rows[0].id == instance_id(rule, date(2026, 11, 5))


def test_footer_month_and_year_balances():
  rows = [
    make_tx(id="e0001", date_str="2026-09-25", amount=-200.0),
    make_tx(id="e0002", date_str="2026-10-10", amount=-300.0),
    make_tx(id="e0003", date_str="2026-12-24", amount=500.0, category="Inkomsten"),
  ]
  walk = _walk(rows, 1000.0, 5000.0)
  lines = _footer_lines(walk, 1000.0, 5000.0, date(2026, 9, 18))
  assert lines[0] == ("Projectie per 30-09-2026 (einde maand): checking €800,00 · spaar €5.000,00")
  assert lines[1] == ("Projectie per 31-12-2026 (einde jaar): checking €1.000,00 · spaar €5.000,00")


def test_footer_lists_crossings_and_deepest_point():
  rows = [
    make_tx(id="e0001", date_str="2026-10-01", amount=-1200.0),  # -> -200
    make_tx(id="e0002", date_str="2026-10-15", amount=300.0, category="Inkomsten"),  # -> 100
    make_tx(id="e0003", date_str="2026-11-05", amount=-400.0),  # -> -300
    make_tx(id="e0004", date_str="2026-12-01", amount=800.0, category="Inkomsten"),  # -> 500
  ]
  walk = _walk(rows, 1000.0, 5000.0)
  lines = _footer_lines(walk, 1000.0, 5000.0, date(2026, 9, 18))
  moment_line = lines[2]
  assert moment_line.startswith("Onder nul (checking):")
  assert "01-10 Huur -200,00" in moment_line
  assert "05-11 Huur -300,00" in moment_line
  assert "diepste punt 05-11 -300,00" in moment_line


def test_footer_no_moments():
  rows = [make_tx(id="e0001", date_str="2026-10-01", amount=-50.0)]
  walk = _walk(rows, 1000.0, 5000.0)
  lines = _footer_lines(walk, 1000.0, 5000.0, date(2026, 9, 18))
  assert lines[2] == "Onder nul: geen momenten tot 31-12-2026."


def test_footer_requires_anchor():
  assert _footer_lines([], None, None, date(2026, 9, 18)) == []


def test_footer_savings_crossing():
  # A positive Overdracht drains savings into checking.
  rows = [
    make_tx(
      id="e0001",
      date_str="2026-11-01",
      amount=1500.0,
      category="Overdracht",
    ),
  ]
  walk = _walk(rows, 5000.0, 1000.0)
  lines = _footer_lines(walk, 5000.0, 1000.0, date(2026, 9, 18))
  savings_line = next(line for line in lines if line.startswith("Onder nul (spaar):"))
  assert "01-11" in savings_line
  assert "-500,00" in savings_line
  assert "diepste punt 01-11 -500,00" in savings_line


def test_footer_moments_capped_at_five():
  # Alternating dip/recover pairs from a 500 start: each -600 dips below
  # zero (crossing), each +600 restores. 7 dips over 13 days.
  rows = []
  for i in range(13):
    rows.append(
      make_tx(
        id=f"e{i:04d}",
        date_str=f"2026-10-{i + 1:02d}",
        amount=-600.0 if i % 2 == 0 else 600.0,
      )
    )
  walk = _walk(rows, 500.0, 5000.0)
  lines = _footer_lines(walk, 500.0, 5000.0, date(2026, 9, 18))
  checking_line = next(line for line in lines if line.startswith("Onder nul (checking):"))
  assert "nog 2 momenten" in checking_line
  assert "diepste punt 01-10 -100,00" in checking_line


def test_anchor_added_row_beats_imported_same_day():
  # Regression: added rows carry source_line=0, imported rows a real line
  # number. A raw source_line key let the imported row outrank the same-day
  # added row, seeding the projection from a stale balance.
  imported = make_tx(
    id="t1412",
    date_str="2026-09-18",
    amount=-20.0,
    status="actual",
  )
  imported.source_line = 100
  imported.balance_checking = 200.00
  added = make_tx(
    id="t1425",
    date_str="2026-09-18",
    amount=11.45,
    status="actual",
  )
  added.source_line = 0
  added.balance_checking = 250.00
  # Added row ranks above imported regardless of list order.
  assert _anchor_row([imported, added], date(2026, 9, 18)) is added
  assert _anchor_row([added, imported], date(2026, 9, 18)) is added


def test_anchor_same_day_added_rows_tie_by_position():
  # Two same-day ADDED rows tie fully (both source_line=0); list position —
  # chain order in the store — decides. This is the real-world case that
  # bare max() got wrong: it kept the FIRST tie, seeding from t1412's
  # balance instead of t1425's.
  first = make_tx(id="t1412", date_str="2026-09-18", amount=-20.0, status="actual")
  second = make_tx(id="t1425", date_str="2026-09-18", amount=11.45, status="actual")
  first.balance_checking = 200.00
  second.balance_checking = 250.00
  assert _anchor_row([first, second], date(2026, 9, 18)) is second
  assert _anchor_row([second, first], date(2026, 9, 18)) is first


def test_anchor_ignores_future_and_summary_rows():
  future = make_tx(id="t0001", date_str="2026-12-31", status="actual")
  future.source_line = 50
  future.balance_checking = 999.0
  landed = make_tx(id="t0003", date_str="2026-09-17", status="actual")
  landed.source_line = 5
  landed.balance_checking = 200.0
  assert _anchor_row([future, landed], date(2026, 9, 18)) is landed


# --- openstaand: the landed-but-unconfirmed middle section -----------------


def test_split_open_walk_rows_by_date():
  """All uncommitted rows dated on or before today form the openstaand
  section; anything later is the projection — both keep walk order."""
  rows = [
    make_tx(id="e0001", date_str="2026-09-01", amount=-100.0),  # overdue
    make_tx(id="e0002", date_str="2026-09-18", amount=-50.0),  # today
    make_tx(id="r:aaaa", date_str="2026-09-19", amount=-800.0),  # future
  ]
  walk = _walk(rows, 1000.0, 5000.0)
  open_rows, future = _split_open(walk, date(2026, 9, 18))
  assert [t.id for t, _c, _s in open_rows] == ["e0001", "e0002"]
  assert [t.id for t, _c, _s in future] == ["r:aaaa"]


def test_print_ledger_view_shows_openstaand_section(tmp_path, monkeypatch, capsys):
  """End-to-end render: an anchor committed at 500, an overdue expected
  row and a landed rule instance both land in the openstaand section;
  the future instance stays in the projection. The rule's expansion
  window reaches back to the anchor (the 09-15 instance appears even
  though it predates today)."""
  from financials import ledger_view as lv

  today = date(2026, 9, 21)
  monkeypatch.setattr(lv, "date", _pinned_date(today))

  console = _wide_console()
  monkeypatch.setattr(lv, "console", console)

  journal = Journal(tmp_path / "journal.jsonl")
  monkeypatch.setattr(lv, "command_journal", lambda: journal)
  monkeypatch.setattr("financials.model.expected_file", lambda: tmp_path / "expected.json")
  monkeypatch.setattr(
    "financials.recurrences.recurrences_file", lambda: tmp_path / "recurrences.json"
  )

  # Anchor: committed salary on 2026-09-10 (balance 1000).
  cmd_add("2026-09-10", "Salaris", "Inkomsten", 1000.0, journal=journal)
  # Overdue expected entry (landed 09-05, never confirmed).
  save_expected(
    [
      Transaction(
        id="e9001",
        date="2026-09-05",
        description="Cadeau",
        category_raw="Uitgaven",
        category="Uitgaven",
        subcategory_raw="",
        subcategory="",
        amount_eur=-50.0,
        status=STATUS_EXPECTED,
        linked_id="",
        source_line=0,
        note="",
      )
    ]
  )
  # Monthly rule on the 15th (started long ago): 09-15 is in the gap.
  save_recurrences(
    [
      Recurrence(
        description="Huur",
        category="Uitgaven",
        amount=-800.0,
        frequency="monthly",
        day=15,
        start="2026-01-01",
      )
    ]
  )

  lv.print_ledger_view(days=30, project=30)
  out = console.file.getvalue()  # the wide console's StringIO buffer
  assert "OPEN" in out
  assert "PROJECTIE" in out
  # gap instance + overdue expected in the open section
  assert "2026-09-05" in out and "2026-09-15" in out
  # October's instance stays in the projection (dim)
  assert "2026-10-15" in out


def test_open_section_reaches_back_past_an_out_of_order_anchor(tmp_path, monkeypatch):
  """Regression (owner report, 2026-09-26): a monthly rule on the 24th is
  unconfirmed, but a LATER-dated row (09-25) is committed first — the
  anchor moved past the instance, which hid it from OPEN. The grace
  lookback (anchor − 35d) keeps it visible and superseding still filters
  a covered instance."""
  from financials import ledger_view as lv

  today = date(2026, 9, 26)
  monkeypatch.setattr(lv, "date", _pinned_date(today))

  console = _wide_console()
  monkeypatch.setattr(lv, "console", console)

  journal = Journal(tmp_path / "journal.jsonl")
  monkeypatch.setattr(lv, "command_journal", lambda: journal)
  monkeypatch.setattr("financials.model.expected_file", lambda: tmp_path / "expected.json")
  monkeypatch.setattr(
    "financials.recurrences.recurrences_file", lambda: tmp_path / "recurrences.json"
  )

  # Committed history: the 08-24 instance WAS confirmed in August (the
  # superseding proof), then a later-dated 09-25 row lands first.
  cmd_add("2026-08-24", "Huur", "Inkomsten", 350.0, journal=journal)
  cmd_add("2026-09-25", "Terug Verkoop A", "Uitgaven", 29.99, journal=journal)
  save_recurrences(
    [
      Recurrence(
        description="Huur",
        category="Inkomsten",
        amount=350.0,
        frequency="monthly",
        day=24,
        start="2024-11-24",
      )
    ]
  )

  lv.print_ledger_view(days=5, project=40)
  out = console.file.getvalue()
  # The 09-24 instance (between the 08-24 cover and today) shows in OPEN.
  assert "2026-09-24" in out
  assert "OPEN" in out
  # October's instance stays in the projection.
  assert "2026-10-24" in out
