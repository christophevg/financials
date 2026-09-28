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
from financials.tui import LedgerTUI

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
      # this seed, so no PROJECTIE section.
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
      await pilot.press("k")  # already at the top: stays
      assert table.cursor_row == 0
      await pilot.press("end")
      assert table.cursor_row == last
      await pilot.press("j")  # already at the bottom: stays
      assert table.cursor_row == last
      await pilot.press("home")
      assert table.cursor_row == 0

  asyncio.run(scenario())