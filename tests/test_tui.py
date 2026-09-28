"""Tests for the TUI (step 1): build_view knobs + pilot-driven app tests.

Store isolation: the TUI flow reads journal + expected + recurrences +
groups. Conftest's autouse fixture covers journal + expected; the tests
isolate the other two stores here. Today is passed into the app
explicitly (LedgerTUI(today=...)), so no date patching is needed.
"""

from __future__ import annotations

import asyncio
from datetime import date

from financials.commands import cmd_add
from financials.journal import Journal
from financials.ledger_view import build_view
from financials.model import STATUS_EXPECTED, Transaction, save_expected
from financials.recurrences import Recurrence, save_recurrences
from financials.tui import KIND_COMMITTED, DetailScreen, LedgerTUI

TEST_TODAY = date(2026, 9, 28)


def _isolated(monkeypatch, tmp_path) -> Journal:
  """Point every store the TUI flow reads at tmp_path."""
  from financials import ledger_view as lv

  journal = Journal(tmp_path / "journal.jsonl")
  monkeypatch.setattr(lv, "command_journal", lambda: journal)
  monkeypatch.setattr(
    "financials.recurrences.recurrences_file", lambda: tmp_path / "recurrences.json"
  )
  monkeypatch.setattr("financials.model.expected_file", lambda: tmp_path / "expected.json")
  monkeypatch.setattr("financials.model.groups_file", lambda: tmp_path / "groups.json")
  return journal


# --- build_view knobs --------------------------------------------------------


def test_days_back_bounds_the_actuals_window(tmp_path, monkeypatch):
  """days_back=3 keeps exactly [today-2, today]; older rows stay out."""
  journal = _isolated(monkeypatch, tmp_path)
  cmd_add("2026-09-24", "Te oud", "Uitgaven", -10.0, journal=journal)
  cmd_add("2026-09-26", "Huur", "Uitgaven", -550.0, journal=journal)
  cmd_add("2026-09-27", "Boodschappen", "Uitgaven", -65.0, journal=journal)
  view = build_view(days=30, project=0, days_back=3, today=TEST_TODAY)
  names = [t.description for t in view.actuals]
  assert "Huur" in names and "Boodschappen" in names and "Te oud" not in names


def test_days_back_default_keeps_the_list_window(tmp_path, monkeypatch):
  """Without days_back the window is exactly `days` (the list behavior)."""
  journal = _isolated(monkeypatch, tmp_path)
  cmd_add("2026-09-10", "Salaris", "Inkomsten", 2000.0, journal=journal)
  view = build_view(days=3, project=0, today=TEST_TODAY)
  assert view.actuals == []


def test_projection_horizon_extends_the_cut(tmp_path, monkeypatch):
  """projection_horizon=year end keeps December instances that a
  project=0 cut would drop."""
  journal = _isolated(monkeypatch, tmp_path)
  cmd_add("2026-09-20", "Salaris", "Inkomsten", 2000.0, journal=journal)
  save_recurrences(
    [
      Recurrence(
        description="Huur",
        category="Uitgaven",
        amount=-550.0,
        frequency="monthly",
        day=1,
        start="2026-01-01",
      )
    ]
  )
  view = build_view(
    days=3,
    project=0,
    projection_horizon=date(2026, 12, 31),
    today=TEST_TODAY,
  )
  future_dates = [row[0].date for row in view.future]
  assert "2026-10-01" in future_dates and "2026-12-01" in future_dates


# --- the Textual app (pilot) ---------------------------------------------------


def _seed(journal: Journal) -> None:
  cmd_add("2026-09-26", "Huur", "Uitgaven", -550.0, journal=journal)
  cmd_add("2026-09-27", "Boodschappen", "Uitgaven", -65.0, journal=journal)


def test_app_renders_rows_and_sections(tmp_path, monkeypatch):
  journal = _isolated(monkeypatch, tmp_path)
  _seed(journal)
  save_expected(
    [
      Transaction(
        id="e9001",
        date="2026-09-27",
        description="Cadeau",
        category="Uitgaven",
        amount_eur=-25.0,
        status=STATUS_EXPECTED,
      )
    ]
  )

  async def scenario() -> None:
    app = LedgerTUI(days_back=3, today=TEST_TODAY)
    async with app.run_test():
      table = app.query_one("#ledger-table")
      # 2 actuals + 1 section separator (OPEN) + 1 open row; no rules in
      # this seed, so no PROJECTIE section. No history rows in this seed
      # (both rows are inside the 3-day window).
      assert table.row_count == 4
      assert "e9001" in table.row_ids
      assert None in table.row_ids  # separators are not entries

  asyncio.run(scenario())


def test_cursor_moves_and_clamps(tmp_path, monkeypatch):
  journal = _isolated(monkeypatch, tmp_path)
  _seed(journal)

  async def scenario() -> None:
    app = LedgerTUI(days_back=3, today=TEST_TODAY)
    async with app.run_test() as pilot:
      table = app.query_one("#ledger-table")
      last = table.row_count - 1
      await pilot.pause()  # let the deferred initial-position callback run
      # Initial position: no projection in this seed → the anchor is
      # the last row (cursor starts there, viewport from the top).
      assert table.cursor_row == last
      await pilot.press("k")  # one row up
      assert table.cursor_row == last - 1
      await pilot.press("j")  # back down, clamped at the bottom
      await pilot.press("j")
      assert table.cursor_row == last
      await pilot.press("home")
      assert table.cursor_row == 0
      await pilot.press("end")
      assert table.cursor_row == last

  asyncio.run(scenario())


def test_history_scrolls_in_above_the_window(tmp_path, monkeypatch):
  """The table holds the full committed history; the cursor anchors on
  the first actual row (the default window), and k/↑ from there walks
  back into history down to the first transaction."""
  journal = _isolated(monkeypatch, tmp_path)
  # One row older than the 3-day window, one inside it.
  cmd_add("2026-09-20", "Salaris", "Inkomsten", 2000.0, journal=journal)
  cmd_add("2026-09-27", "Boodschappen", "Uitgaven", -65.0, journal=journal)

  async def scenario() -> None:
    app = LedgerTUI(days_back=3, today=TEST_TODAY)
    async with app.run_test() as pilot:
      table = app.query_one("#ledger-table")
      await pilot.pause()  # let the deferred initial-position callback run
      # 1 history row (09-20) + 1 actual (09-27); no separators (no
      # open rows, no projection in this seed). Journaled ids are
      # content hashes; assert the Datum column per position instead.
      assert table.row_count == 2
      assert table.get_row_at(0)[0].plain == "2026-09-20"
      assert table.get_row_at(1)[0].plain == "2026-09-27"
      # No PROJECTIE section here: the anchor is the last row, so the
      # cursor starts there (the 10-rows-above rule clamps at the top).
      assert table.cursor_row == 1
      # Scroll up into history, clamped at the first transaction.
      await pilot.press("k")
      assert table.cursor_row == 0
      await pilot.press("k")
      assert table.cursor_row == 0

  asyncio.run(scenario())


def test_initial_position_anchored_on_projection(tmp_path, monkeypatch):
  """On open, the PROJECTIE separator sits at the bottom of the
  viewport with the 10 rows above it starting at the top; the cursor
  rests on the PROJECTIE row."""
  journal = _isolated(monkeypatch, tmp_path)
  # Older history (3 rows) + actuals (2 rows) + an expected entry (the
  # OPEN section) + a monthly rule (the PROJECTIE section).
  cmd_add("2026-08-01", "Oud", "Uitgaven", -10.0, journal=journal)
  cmd_add("2026-09-26", "Huur", "Uitgaven", -550.0, journal=journal)
  cmd_add("2026-09-27", "Boodschappen", "Uitgaven", -65.0, journal=journal)
  save_expected(
    [
      Transaction(
        id="e9001",
        date="2026-09-27",
        description="Cadeau",
        category="Uitgaven",
        amount_eur=-25.0,
        status=STATUS_EXPECTED,
      )
    ]
  )
  save_recurrences(
    [
      Recurrence(
        description="Sport",
        category="Uitgaven",
        amount=-30.0,
        frequency="monthly",
        day=5,
        start="2026-01-01",
      )
    ]
  )

  async def scenario() -> None:
    app = LedgerTUI(days_back=3, today=TEST_TODAY)
    async with app.run_test() as pilot:
      table = app.query_one("#ledger-table")
      await pilot.pause()  # let the deferred initial-position callback run
      # Rows: 1 history + 2 actuals + 1 OPEN sep + 2 open rows (e9001
      # + the rule's 09-05 instance inside the grace lookback)
      # + 1 PROJECTIE sep + instances to year end (10-05, 11-05, 12-05)
      # = 10 rows; the separator (index 7) is the anchor.
      assert table.row_count == 10
      # The anchor: the PROJECTIE separator's row index.
      assert table.cursor_row == table.get_row_index("sep-projection")
      assert table.get_row_at(table.cursor_row)[1].plain == "PROJECTIE"
      # Line-based positioning: 11 lines above the anchor (2-line
      # rows), minus the 10-line context → top_line 1, snapped to a
      # row start → 0 (row 0 spans lines 0-1).
      assert table.scroll_y == 0.0

  asyncio.run(scenario())


def test_enter_opens_detail_dialog(tmp_path, monkeypatch):
  """Enter on an entry row opens the readonly DetailScreen with the
  entry's data and its provenance; Esc closes it back to the table."""
  journal = _isolated(monkeypatch, tmp_path)
  cmd_add("2026-09-27", "Boodschappen", "Uitgaven", -65.0, journal=journal)

  async def scenario() -> None:
    app = LedgerTUI(days_back=3, today=TEST_TODAY)
    async with app.run_test() as pilot:
      table = app.query_one("#ledger-table")
      await pilot.pause()
      # Land on the actual row (no projection in this seed → last row).
      table.move_cursor(row=table.row_count - 1, scroll=False)
      await pilot.press("enter")
      await pilot.pause()
      # The dialog is the active screen with the entry's details.
      dialog = app.screen
      assert isinstance(dialog, DetailScreen)
      assert dialog._entry[0].description == "Boodschappen"
      assert dialog._kind == KIND_COMMITTED
      await pilot.press("escape")
      await pilot.pause()
      assert app.screen is not dialog  # back on the ledger view

  asyncio.run(scenario())


def test_enter_on_separator_is_a_noop(tmp_path, monkeypatch):
  """Enter on a section separator row (no entry behind it) does
  nothing: no dialog, still the ledger view."""
  journal = _isolated(monkeypatch, tmp_path)
  cmd_add("2026-09-27", "Boodschappen", "Uitgaven", -65.0, journal=journal)
  save_expected(
    [
      Transaction(
        id="e9001",
        date="2026-09-27",
        description="Cadeau",
        category="Uitgaven",
        amount_eur=-25.0,
        status=STATUS_EXPECTED,
      )
    ]
  )

  async def scenario() -> None:
    app = LedgerTUI(days_back=3, today=TEST_TODAY)
    async with app.run_test() as pilot:
      table = app.query_one("#ledger-table")
      await pilot.pause()
      sep_row = table.get_row_index("sep-open")
      table.move_cursor(row=sep_row, scroll=False)
      await pilot.press("enter")
      await pilot.pause()
      assert not isinstance(app.screen, DetailScreen)

  asyncio.run(scenario())
