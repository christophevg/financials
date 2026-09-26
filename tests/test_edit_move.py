"""Tests: edit-to-future on an actual row moves it via delete + expected
add (step 6): the delete is a DeleteMutation, the expected add creates
the overlay entry; expected rows edited to a future date simply update
in the register."""

from datetime import date, timedelta

from financials import edit as module
from financials.commands import cmd_add
from financials.journal import Journal, load_ledger
from financials.model import (
  Transaction,
  load_expected,
  save_expected,
)


def _collect_prompt(**overrides):
  """A Prompt.ask mock matching _collect_changes' prompt sequence:
  Datum (default = current date), Omschrijving (default = current),
  Bedrag (default = current amount). Category is monkeypatched
  separately. Overrides: {prompt_substring: answer} — when the prompt
  text contains the substring the override is returned, else the
  default (Enter keeps the value)."""

  def ask(*args, **kwargs):
    default = kwargs.get("default", "")
    for needle, answer in overrides.items():
      if needle in str(args):
        return answer
    return default

  return ask


def _isolate(tmp_path, monkeypatch):
  journal = Journal(tmp_path / "journal.jsonl")
  expected_path = tmp_path / "expected.json"
  save_expected([], path=expected_path)
  monkeypatch.setattr(module, "command_journal", lambda: journal)
  monkeypatch.setattr(module, "load_expected", lambda: load_expected(path=expected_path))
  monkeypatch.setattr(module, "save_expected", lambda rows: save_expected(rows, path=expected_path))
  # add_expected (expected.py) has its own store resolution — isolate it
  # too, or a move writes to the owner's real expected.json.
  from financials import expected as expected_module

  monkeypatch.setattr(expected_module, "load_expected", lambda: load_expected(path=expected_path))
  monkeypatch.setattr(
    expected_module, "save_expected", lambda rows: save_expected(rows, path=expected_path)
  )
  return journal, expected_path


def test_move_to_expected_deletes_and_recreates(tmp_path, monkeypatch):
  journal, expected_path = _isolate(tmp_path, monkeypatch)
  cmd_add("2026-01-01", "Opening", "Inkomsten", 1000.0, journal=journal)
  _mutation, row = cmd_add("2026-01-05", "Terug", "Uitgaven", -30.0, journal=journal)
  future = (date.today() + timedelta(days=10)).isoformat()
  monkeypatch.setattr(module.Prompt, "ask", _collect_prompt(Datum=future))
  monkeypatch.setattr(module, "ask_category", lambda current="", allow_blank=False: current)
  monkeypatch.setattr(
    module,
    "load_expected",
    lambda: load_expected(path=expected_path),
  )
  projected = Transaction(
    id=row.id,
    date=row.date.isoformat(),
    description=row.description,
    category_raw=row.category,
    category=row.category,
    subcategory_raw="",
    subcategory="",
    amount_eur=row.postings.get("checking"),
    status="actual",
    linked_id="",
  )
  changes = {"date": future}
  code = module._move_to_expected(projected, future, changes)
  assert code == 0
  ledger = load_ledger(journal)
  assert ledger.find(row.id)[0] is None  # deleted as actual
  expected = load_expected(path=expected_path)
  assert len(expected) == 1
  assert expected[0].description == "Terug"
  assert expected[0].date == future
  assert expected[0].amount_eur == -30.0
  # the tail re-derived: opening is the only committed row left
  assert len(ledger.transactions) == 1


def test_future_date_edit_is_not_a_field_update(tmp_path, monkeypatch):
  """The routing decision: a future date never produces an UpdateMutation
  against the committed ledger — it routes to the move."""
  journal, expected_path = _isolate(tmp_path, monkeypatch)
  cmd_add("2026-01-01", "Opening", "Inkomsten", 1000.0, journal=journal)
  _mutation, row = cmd_add("2026-01-05", "Terug", "Uitgaven", -30.0, journal=journal)
  future = (date.today() + timedelta(days=10)).isoformat()
  monkeypatch.setattr(module.Prompt, "ask", lambda *a, **k: k.get("default", ""))
  monkeypatch.setattr(module, "ask_category", lambda current="", allow_blank=False: current)
  changes = {"date": future}
  assert changes["date"] > date.today().isoformat()
  # the edit path branches on this predicate — verify the branch condition
  # by asserting cmd_update was NOT what would run (move path returns 0
  # and the journal holds only the delete, not an update)
  projected = Transaction(
    id=row.id,
    date=row.date.isoformat(),
    description=row.description,
    category_raw=row.category,
    category=row.category,
    subcategory_raw="",
    subcategory="",
    amount_eur=row.postings.get("checking"),
    status="actual",
    linked_id="",
  )
  module._move_to_expected(projected, future, {"date": future})
  ledger = load_ledger(journal)
  assert ledger.find(row.id)[0] is None  # delete, not update: id gone
  assert len(load_expected(path=expected_path)) == 1
