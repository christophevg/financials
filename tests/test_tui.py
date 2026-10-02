"""Tests for the TUI (step 1): build_view knobs + pilot-driven app tests.

Store isolation: the TUI flow reads journal + expected + recurrences +
groups. Conftest's autouse fixture covers journal + expected; the tests
isolate the other two stores here. Today is passed into the app
explicitly (LedgerTUI(today=...)), so no date patching is needed. The
add flow (tui/add.py) additionally READS the config (approved
categories) — patched per-test (categories can be personal).
"""

from __future__ import annotations

import asyncio
from datetime import date

from financials.commands import cmd_add
from financials.config import FinancialsConfig
from financials.journal import Journal
from financials.ledger_view import build_view
from financials.model import STATUS_EXPECTED, Transaction, save_expected
from financials.recurrences import Recurrence, save_recurrences
from financials.tui import KIND_COMMITTED, DetailScreen, LedgerTUI
from financials.tui.add import (
  _FIELD_ORDER,
  AddScreen,
  CategoryAutocomplete,
  _Field,
)

TEST_TODAY = date(2026, 9, 28)


def _pin_expected_today(monkeypatch) -> None:
  """Pin the TODAY-clock of the EXPECTED store's future gate (expected.
  add_expected reads its own module-global datetime.date) to TEST_TODAY:
  both the AddScreen's future-dispatch check and add_expected's gate must
  agree, or a save the screen accepts is refused by the gate (a literal
  future date goes stale relative to the real clock)."""
  FakeDate = type(
    "FakeDate",
    (date,),
    {"today": classmethod(lambda cls: TEST_TODAY)},
  )
  monkeypatch.setattr("financials.expected.date", FakeDate)


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


def _categories(monkeypatch, tmp_path) -> None:
  """Pinned category list (config can be personal — never live)."""
  monkeypatch.setattr(
    "financials.config.get_config",
    lambda: FinancialsConfig(
      categories=["Inkomsten", "Uitgaven", "Overdracht"], data_dir=tmp_path
    ),
  )


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


# --- step 3: the add dialog ---------------------------------------------------


def test_a_opens_add_dialog(tmp_path, monkeypatch):
  """`a` from the ledger view opens the AddScreen (date pre-filled with
  today, focus on the date field); Esc cancels back to the ledger."""
  journal = _isolated(monkeypatch, tmp_path)
  _categories(monkeypatch, tmp_path)
  _seed(journal)

  async def scenario() -> None:
    app = LedgerTUI(days_back=3, today=TEST_TODAY)
    async with app.run_test() as pilot:
      await pilot.press("a")
      await pilot.pause()
      screen = app.screen
      assert isinstance(screen, AddScreen)
      assert screen._fields["add-date"].value == TEST_TODAY.isoformat()
      assert screen._fields["add-date"].has_focus
      await pilot.press("escape")
      await pilot.pause()
      assert not isinstance(app.screen, AddScreen)

  asyncio.run(scenario())


def test_add_dialog_layout_pairs_are_unshifted(tmp_path, monkeypatch):
  """The autocomplete floats at SCREEN level (overlay): the form grid
  holds exactly 5 label+input pairs, one row each, all inputs in the
  same column. Regression gate: a grid child autocomplete consumed a
  cell and shifted every pair after Categorie (owner screenshot). Also
  locks the 5-row cap (library default is 12): six categories in the
  config → the list shows 5 (auto-shrinks below the cap)."""
  journal = _isolated(monkeypatch, tmp_path)
  # Six categories so the 5-row cap actually engages (3 would
  # auto-shrink the list below it).
  monkeypatch.setattr(
    "financials.config.get_config",
    lambda: FinancialsConfig(
      categories=["Aaa", "Bbb", "Ccc", "Ddd", "Eee", "Uitgaven"],
      data_dir=tmp_path,
    ),
  )
  _seed(journal)

  async def scenario() -> None:
    app = LedgerTUI(days_back=3, today=TEST_TODAY)
    async with app.run_test() as pilot:
      await pilot.press("a")
      await pilot.pause()
      screen = app.screen
      assert isinstance(screen, AddScreen)
      # Exactly 10 in-flow grid children (2 per field row) — no
      # autocomplete riding a row.
      children = screen.query("#add-grid > *")
      assert len(children) == 10, (
        f"grid children: {[w.id or type(w).__name__ for w in children]}"
      )
      # The autocomplete exists, at screen level (not a grid child).
      assert screen.query_one("#add-category-ac") is not None
      assert not screen.query("#add-grid > #add-category-ac")
      # The five inputs sit in five distinct rows, in field order, all
      # in the same (value) column.
      inputs = [screen.query_one(f"#{fid}", _Field) for fid in _FIELD_ORDER]
      ys = [inp.region.y for inp in inputs]
      assert ys == sorted(ys) and len(set(ys)) == 5
      assert len({inp.region.x for inp in inputs}) == 1
      # Out-of-flow overlay: opening the dropdown must not move the
      # form (the form shifted up when the dropdown grew the centered
      # auto-height dialog before `position: absolute`).
      dialog = screen.query_one("#add-dialog")
      y_before = dialog.region.y
      ac = screen.query_one("#add-category-ac", CategoryAutocomplete)
      screen._fields["add-category"].focus()
      await pilot.pause()
      assert ac.display  # dropdown open
      assert dialog.region.y == y_before  # the form did not move
      # Visible rows capped at 1 (owner tuning, 2026-09-28: only the
      # best fuzzy match shows — zero vertical shift in the real
      # terminal, and typing 1-2 letters + Enter enters the category
      # fastest; the library default was 12).
      assert ac.option_list.size.height == 1

  asyncio.run(scenario())


def test_add_dialog_saves_and_refocuses_new_row(tmp_path, monkeypatch):
  """The happy path: fill the form, submit — the journal gains the
  entry, the dialog closes, and the cursor lands on the NEW row."""
  journal = _isolated(monkeypatch, tmp_path)
  _categories(monkeypatch, tmp_path)
  _seed(journal)

  async def scenario() -> None:
    app = LedgerTUI(days_back=3, today=TEST_TODAY)
    async with app.run_test() as pilot:
      await pilot.press("a")
      await pilot.pause()
      screen = app.screen
      assert isinstance(screen, AddScreen)
      screen._fields["add-description"].value = "Kafe"
      screen._fields["add-category"].value = "Uitgaven"
      screen._fields["add-amount"].value = "-38.93"
      await pilot.pause()
      # Enter walks date → description → category (exact value, the
      # dropdown is hidden → Submitted → accepted) → amount; Enter on
      # the saldo field submits (blank = accept the projection).
      await pilot.press("enter", "enter", "enter", "enter", "enter")
      await pilot.pause()
      assert not isinstance(app.screen, AddScreen)  # dialog closed
      table = app.query_one("#ledger-table")
      new_id = table.selected_id()
      assert new_id is not None
      # The cursor sits on the new row: Kafe / Uitgaven, -38.93.
      cells = table.get_row_at(table.cursor_row)
      assert cells[0].plain == TEST_TODAY.isoformat()
      assert "Kafe" in cells[1].plain and "Uitgaven" in cells[1].plain
      assert cells[2].plain == "-38.93"
      # The journal holds the new entry.
      from financials.journal import load_ledger

      ledger = load_ledger(journal)
      assert ledger.find(new_id)[0].description == "Kafe"

  asyncio.run(scenario())


def test_add_category_dropdown_fuzzy_completes_and_advances(tmp_path, monkeypatch):
  """The real category path: focus the field → the dropdown offers ALL
  approved categories (empty input); typing narrows fuzzily; Enter
  completes the highlighted match and advances to Bedrag; finishing
  the form saves the entry."""
  journal = _isolated(monkeypatch, tmp_path)
  _categories(monkeypatch, tmp_path)
  _seed(journal)

  async def scenario() -> None:
    app = LedgerTUI(days_back=3, today=TEST_TODAY)
    async with app.run_test() as pilot:
      await pilot.press("a")
      await pilot.pause()
      screen = app.screen
      assert isinstance(screen, AddScreen)
      await pilot.press("enter", "enter")  # date → description → category
      await pilot.pause()
      ac = screen.query_one("#add-category-ac", CategoryAutocomplete)
      # Empty input + focus → the full approved list is offered.
      assert ac.display
      assert ac.option_list.option_count == 3
      await pilot.press("u", "i", "t")  # fuzzy: only Uitgaven matches
      await pilot.pause()
      assert ac.option_list.option_count == 1
      screen._fields["add-description"].value = "Kafe"  # still blank
      await pilot.press("enter")  # complete + advance (library intercept)
      await pilot.pause()
      assert screen._fields["add-category"].value == "Uitgaven"
      assert screen._fields["add-amount"].has_focus
      # Finish the form and submit.
      screen._fields["add-amount"].value = "-38.93"
      await pilot.press("enter", "enter")
      await pilot.pause()
      assert not isinstance(app.screen, AddScreen)
      from financials.journal import load_ledger

      assert any(
        t.description == "Kafe" for t in load_ledger(journal).transactions
      )

  asyncio.run(scenario())


def test_add_escape_hides_dropdown_then_cancels(tmp_path, monkeypatch):
  """Two-stage escape: Esc with the dropdown visible hides it (dialog
  stays); a second Esc cancels the dialog with nothing saved."""
  journal = _isolated(monkeypatch, tmp_path)
  _categories(monkeypatch, tmp_path)
  _seed(journal)

  async def scenario() -> None:
    app = LedgerTUI(days_back=3, today=TEST_TODAY)
    async with app.run_test() as pilot:
      await pilot.press("a")
      await pilot.pause()
      screen = app.screen
      assert isinstance(screen, AddScreen)
      await pilot.press("enter", "enter")  # walk to the category field
      await pilot.pause()
      ac = screen.query_one("#add-category-ac", CategoryAutocomplete)
      assert ac.display  # the dropdown is open
      await pilot.press("escape")
      await pilot.pause()
      assert not ac.display  # hidden only — the dialog stays open
      assert isinstance(app.screen, AddScreen)
      await pilot.press("escape")
      await pilot.pause()
      assert not isinstance(app.screen, AddScreen)
      # Nothing saved (the seed's 2 rows only).
      from financials.journal import load_ledger

      assert len(load_ledger(journal).transactions) == 2

  asyncio.run(scenario())


def test_add_dialog_rejects_bad_category_then_saves(tmp_path, monkeypatch):
  """A non-approved category shows an inline error and saves nothing;
  after fixing the field, resubmitting saves."""
  journal = _isolated(monkeypatch, tmp_path)
  _categories(monkeypatch, tmp_path)
  _seed(journal)

  async def scenario() -> None:
    app = LedgerTUI(days_back=3, today=TEST_TODAY)
    async with app.run_test() as pilot:
      await pilot.press("a")
      await pilot.pause()
      screen = app.screen
      assert isinstance(screen, AddScreen)
      screen._fields["add-description"].value = "Kafe"
      screen._fields["add-category"].value = "NietGoedgekeurd"
      screen._fields["add-amount"].value = "-5.00"
      await pilot.pause()
      await pilot.press("enter", "enter", "enter", "enter", "enter")
      await pilot.pause()
      # Still open, nothing saved: the category step refused (not an
      # approved category) — the seed's 2 rows are unchanged (no
      # expected content in this seed → no OPEN section).
      assert isinstance(app.screen, AddScreen)
      table = app.query_one("#ledger-table")
      assert len(table.row_ids) == 2
      # Fix the category; focus is still on the category field — Enter
      # accepts the exact value, then walks on.
      screen._fields["add-category"].value = "Uitgaven"
      await pilot.pause()
      await pilot.press("enter", "enter", "enter")
      await pilot.pause()
      assert not isinstance(app.screen, AddScreen)
      assert "Kafe" in table.get_row_at(table.cursor_row)[1].plain

  asyncio.run(scenario())


def test_add_dialog_future_date_goes_to_expected(tmp_path, monkeypatch):
  """A future date routes to the expected register (no journal entry):
  the dialog closes, the new expected row sits in PROJECTIE, cursor on
  it, and the journal is untouched."""
  journal = _isolated(monkeypatch, tmp_path)
  _categories(monkeypatch, tmp_path)
  _seed(journal)

  async def scenario() -> None:
    app = LedgerTUI(days_back=3, today=TEST_TODAY)
    async with app.run_test() as pilot:
      await pilot.press("a")
      await pilot.pause()
      screen = app.screen
      assert isinstance(screen, AddScreen)
      # Pin add_expected's future gate to TEST_TODAY: the date below is NOT a
      # rule instance (a computed date would drift out of the pinned
      # projection horizon once the real clock advances ~3 months), and the
      # gate must agree with the screen's own future-dispatch (pinned
      # today), else the save the screen accepts is refused.
      _pin_expected_today(monkeypatch)
      screen._fields["add-date"].value = "2026-10-01"
      screen._fields["add-description"].value = "Verjaardag"
      screen._fields["add-category"].value = "Uitgaven"
      screen._fields["add-amount"].value = "-100.00"
      await pilot.press("enter", "enter", "enter", "enter", "enter")
      await pilot.pause()
      assert not isinstance(app.screen, AddScreen)
      table = app.query_one("#ledger-table")
      new_id = table.selected_id()
      assert new_id is not None and new_id.startswith("e")
      # The cursor sits on the new row in the projection section.
      cells = table.get_row_at(table.cursor_row)
      assert cells[0].plain == "2026-10-01"
      assert "Verjaardag" in cells[1].plain
      # The journal is untouched (expected is a separate store).
      from financials.journal import load_ledger

      assert all(
        t.description != "Verjaardag"
        for t in load_ledger(journal).transactions
      )

  asyncio.run(scenario())


def test_add_dialog_escape_aborts_midway(tmp_path, monkeypatch):
  """Esc from a field cancels with nothing saved (the CLI's 'q') — the
  app itself does NOT quit (the field's escape binding wins over the
  app's quit binding)."""
  journal = _isolated(monkeypatch, tmp_path)
  _categories(monkeypatch, tmp_path)
  _seed(journal)

  async def scenario() -> None:
    app = LedgerTUI(days_back=3, today=TEST_TODAY)
    async with app.run_test() as pilot:
      await pilot.press("a")
      await pilot.pause()
      screen = app.screen
      assert isinstance(screen, AddScreen)
      screen._fields["add-description"].value = "Kafe"
      await pilot.press("enter")  # move off the date field
      await pilot.pause()
      await pilot.press("escape")  # cancel mid-form
      await pilot.pause()
      # Back on the ledger's default screen (no quit, no dialog).
      assert app.screen is app.screen_stack[0]
      from financials.journal import load_ledger

      assert all(t.description != "Kafe" for t in load_ledger(journal).transactions)

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
